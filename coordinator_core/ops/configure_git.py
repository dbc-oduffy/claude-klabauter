"""
coordinator_core.ops.configure_git — ported from
coordinator/bin/coordinator-configure-git (DOE-PORT bin-entrypoint variant).

Purpose: applies coordinator's git-config hardening to a repo — five content-neutral,
idempotent settings, three of them machine-wide and Windows-only:

1. `gc.auto=0`: git's default auto-gc/maintenance DETACHES into a background process
   on Git-for-Windows, and that detached child is the likely concurrent handle-holder
   that blocks a commit's `index.lock` cleanup under concurrent-EM sessions. The
   predecessor of this setting, `gc.autoDetach false`, tried to fix that by making
   auto-gc run SYNCHRONOUSLY in the foreground instead — but on a box sharing one
   worktree across ~50 sessions that turned into RACING foreground repacks, the
   mechanism that produced 1.1 GB of orphaned `.tmp-*-pack-*` pack bodies. `gc.auto=0`
   removes the producer instead of reshaping it: auto-gc is turned OFF outright, not
   moved to the foreground and not run synchronously by anything. The maintenance job
   this setting used to do automatically is now done deliberately instead, from a
   ceremony, by `coordinator_core.ops.git_maintenance`. See `_SETTINGS`'s own inline
   comment for the `maintenance.autoDetach` fallback coupling this key also governs,
   and for the two-writer rollout window that coupling creates.

2. `core.checkStat minimal`: Git-for-Windows records nanosecond mtimes in the index, but
   NTFS reports them back at coarser precision, and the default `checkStat` also
   compares the unstable ctime/ino/dev fields — so under concurrent-EM sessions
   continuously rewriting the shared .git/index, entries perpetually re-flag as "racy"
   and `git status` reports a phantom dirty tree. `minimal` compares only mtime+size,
   which are stable across the NTFS round-trip, curing the phantom-dirty churn. Benign
   and content-neutral on every platform (no-op effect where ctime/ino are already
   stable).

3. Help-browser triple (`help.format=web`, `web.browser=noop`,
   `browser.noop.cmd=echo not-opening-browser-for:`): `git help --web` defaults to
   launching the operator's OS browser on every doc lookup; this group redirects it to
   a no-op printer instead. Windows-only (`sys.platform == "win32"`) and machine-wide
   (`scope="global"`) regardless of invocation mode, and skipped as a whole group when
   the operator has already set `web.browser` themselves — an operator who named their
   own browser meant it.

Behavior contract: per-repo (local git config) by default; `--global` sets the
machine-wide default instead. Idempotent — re-running with matching config already set
is a no-op (reported on stderr, exit 0). Exit codes: 0 — configured (or already
correct); 1 — not a git repository (per-repo mode only) or `git config` failed to set a
key. All diagnostic/status output goes to stderr; stdout is unused, matching the bash
oracle's contract. A setting declaring `scope="global"` is always written machine-wide
regardless of invocation mode — per-key scope overrides the CLI's `--global`/no-flag
choice for that key.

Spec backlink: cross-repo/inbox/2026-05-30-index-lock-leak-concurrent-em.md (example-game-repo
consult); docs/wiki/concurrent-em-hazards.md § H21.
Prior bash implementation: coordinator/bin/coordinator-configure-git.

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix" mid-port):
    - Does NOT validate that `--global` mode is run inside a git repository — the bash
      oracle's `git rev-parse --git-dir` guard only fires in per-repo mode; `--global`
      config writes happen regardless of cwd.
    - Does NOT support any flag other than `--global` (or no flag) — any other first
      argument is silently treated as "no flag" (per-repo mode), exactly matching the
      bash oracle's `[[ "${1:-}" == "--global" ]]` single-value comparison.
    - Does NOT batch `git config` calls — invokes `git config --get` then `git config
      <key> <value>` per setting, one subprocess pair per key, matching the oracle's
      per-key loop and its per-key partial-failure exit behavior (a failure on the
      second key exits 1 even if the first key already changed).
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from coordinator_core.win_portability import leaf_spawn_creationflags
from coordinator_core.git.repo_root import git_dir
from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)


@dataclass(frozen=True)
class GitSetting:

    key: str
    value: str
    scope: str = "repo"
    platforms: frozenset[str] | None = None
    group: str | None = None
    unset_group: str | None = None


_SETTINGS: tuple[GitSetting, ...] = (
    # PER-REPO, no scope= — the 2026-08-07 per-key scope ruling recorded in the
    # WHAT REMOVING gc.autoDetach ALSO TURNS OFF, stated because nothing else
    # THE TWO-WRITER ROLLOUT WINDOW, named rather than left implicit: this op
    GitSetting(key="gc.auto", value="0"),
    GitSetting(key="core.checkStat", value="minimal", scope="global"),
    # git-web--browse). Verified working under GIT_CONFIG_* injection ahead of
    GitSetting(
        key="help.format",
        value="web",
        scope="global",
        platforms=frozenset({"win32"}),
        group="help-browser",
        unset_group="help-browser",
    ),
    GitSetting(
        key="web.browser",
        value="noop",
        scope="global",
        platforms=frozenset({"win32"}),
        group="help-browser",
        unset_group="help-browser",
    ),
    GitSetting(
        key="browser.noop.cmd",
        value="echo not-opening-browser-for:",
        scope="global",
        platforms=frozenset({"win32"}),
        group="help-browser",
        unset_group="help-browser",
    ),
)

def _write_surface_reason(setting: GitSetting) -> str:
    """Free-text justification for one `_SETTINGS` record, carried on the
    declared `WriteSurfaceEntry` per the Static-vs-Shaped discriminator:
    this writer's known, literal key set stays Static, and any runtime
    conditionality (platform, group precondition, scope) is carried here
    rather than in the declaration's shape."""
    parts: list[str] = [f"scope={setting.scope}"]
    if setting.platforms is not None:
        platforms = ", ".join(sorted(setting.platforms))
        parts.append(f"written only on platforms: {platforms}")
    if setting.group is not None:
        parts.append(
            f"written only when group {setting.group!r}'s precondition passes "
            "(skipped whole-group if the operator already set a conflicting value)"
        )
    return "; ".join(parts)


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="configure-git",
    source_module="coordinator_core.ops.configure_git",
    clauses=(
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(
                    kind="git-config-key",
                    key=s.key,
                    unset_group=s.unset_group,
                    reason=_write_surface_reason(s),
                )
                for s in _SETTINGS
            ),
        ),
    ),
)
"""This writer's declared write surface — derived FROM `_SETTINGS` (never a
restated literal list), so a future edit to `_SETTINGS` alone cannot make
this declaration under-report without its test going red. See spec
backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md,
chunk C2."""


def _git_config_get(scope: Sequence[str], key: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "config", *scope, "--get", key],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            **leaf_spawn_creationflags(),
        )
    except OSError:
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _git_config_set(scope: Sequence[str], key: str, value: str) -> bool:
    try:
        res = subprocess.run(
            ["git", "config", *scope, key, value],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            **leaf_spawn_creationflags(),
        )
    except OSError:
        return False
    return res.returncode == 0


def _is_git_repo() -> bool:
    return git_dir() is not None


def _help_browser_group_precondition(
    resolve_scope: Callable[[GitSetting], tuple[str, ...]],
) -> tuple[bool, str]:
    probe = GitSetting(key="web.browser", value="noop", scope="global")
    scope = resolve_scope(probe)
    current = _git_config_get(scope, "web.browser")
    if current is not None:
        return False, f"web.browser already set to {current!r}"
    return True, ""


_GROUP_PRECONDITIONS: dict[
    str, Callable[[Callable[[GitSetting], tuple[str, ...]]], tuple[bool, str]]
] = {
    "help-browser": _help_browser_group_precondition,
}


def _resolve_scope(setting: GitSetting, is_global: bool) -> tuple[tuple[str, ...], str]:
    if setting.scope == "global" or is_global:
        return ("--global",), "global"
    return (), "repo"


def main(argv: list[str]) -> int:
    is_global = bool(argv) and argv[0] == "--global"

    if not is_global:
        if not _is_git_repo():
            print("coordinator-configure-git: not a git repository", file=sys.stderr)
            return 1

    group_verdicts: dict[str, bool] = {}
    changed = False
    for setting in _SETTINGS:
        key, want = setting.key, setting.value

        if setting.platforms is not None and sys.platform not in setting.platforms:
            continue

        if setting.group is not None:
            if setting.group not in group_verdicts:
                predicate = _GROUP_PRECONDITIONS.get(setting.group)
                if predicate is None:
                    group_verdicts[setting.group] = True
                else:
                    should_write, reason = predicate(
                        lambda s, _is_global=is_global: _resolve_scope(s, _is_global)[0]
                    )
                    group_verdicts[setting.group] = should_write
                    if not should_write:
                        print(
                            f"coordinator-configure-git: skipping group "
                            f"{setting.group!r}: {reason}",
                            file=sys.stderr,
                        )
            if not group_verdicts[setting.group]:
                continue

        scope, scope_label = _resolve_scope(setting, is_global)
        cur = _git_config_get(scope, key)
        if cur == want:
            continue
        if _git_config_set(scope, key, want):
            print(
                f"coordinator-configure-git: set {scope_label} {key}={want} "
                f"(was: {cur if cur is not None else '<unset>'})",
                file=sys.stderr,
            )
            changed = True
        else:
            print(
                f"coordinator-configure-git: ERROR — failed to set {key}={want}",
                file=sys.stderr,
            )
            return 1

    if not changed:
        scope_label = "global" if is_global else "repo"
        print(
            f"coordinator-configure-git: {scope_label} git config already hardened (no change)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
