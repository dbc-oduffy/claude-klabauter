"""
coordinator_core.plan_assemble.predicates.concurrent_preflight — Layer 0
leaf reader for contract row `:83`, `gates.substrate.concurrent_preflight`.

Purpose: `:83`'s spec text claims a "concurrent-session preflight" already
exists and this row merely consumes it. Verified against `HEAD` while
drafting the wave-2 plan: no `concurrent_session_preflight` symbol under
`coordinator_core/`, no `concurrent-session-preflight` entrypoint in
`coordinator/bin/`, and none on the installed
`${COORDINATOR_SETTINGS_HOME}/bin/` surface — only prose mentions in three
older plans. This module is the build the spec's premise was wrong about,
not a consume.

It checks two legs a plan-drafting EM would otherwise check by hand, and
surfaces the raw hits — it never resolves a verdict. Whether a hit means
"stop and coordinate" or "false alarm, proceed" is a PM-surface judgment
call this module has no authority to make (an untrusted-gate boundary, same
class as every other `*.recommended`/`*.verdict`/`*.fires` row this plan's
Anti-scope forbids resolving).

Leg (a) — today-dated-plan collision: does another plan file dated today
already exist under `docs/plans/`, or does today's git history already
carry a commit touching `docs/plans/`. Either is evidence of a concurrent
plan-drafting session, not proof of one.

Leg (b) — `source_memo:` collision: when this plan's own frontmatter names
a `source_memo:` it is actioning, does any *other* plan file already carry
a `source_memo:` frontmatter key naming that same memo basename (or, failing
a frontmatter hit, mention the memo basename in its body). Either is
evidence another session already routed the same memo.

KNOWN RESIDUAL (record, not silently absorbed): leg (b) catches the
plan-routed collision only — a plan whose frontmatter or body names the
memo. A commit-only or inline realization of a memo (no plan file at all
ever carries `source_memo:`) leaves no trace this leg can see. That case is
caught by a DIFFERENT mechanism entirely: the memo claim-lock at write time,
and the archived memo's own `realized_by` claim-of-record once it lands.
This module does not attempt to widen its search to compensate — doing so
would mean grepping arbitrary commit messages/diffs for memo basenames,
which is exactly the kind of parallel resolution ladder this package's
sibling modules are told not to build.

Negative-spec:
  - Does NOT decide whether a hit blocks, warns, or is a false alarm. Every
    hit is raw evidence in the emitted dict; disposition is a PM-surface
    call downstream, never made here (Anti-scope, this plan).
  - Does NOT search commit diffs, commit messages, or any store other than
    `docs/plans/*.md` file names/content and `git log --since=today` for
    leg (a). No widening to catch the commit-only/inline residual named
    above.
  - Does NOT emit `false` or raise on an unreadable `docs/plans/` directory,
    a missing `.git`, or a plan lacking `source_memo:` — those resolve to
    `undetermined(reason=...)` per this package's one legal sentinel.
  - Does NOT cache results across calls — one `concurrent_preflight(...)`
    call reads disk/git exactly once, no process-lifetime state.
  - Does NOT re-derive `PredicateContext` or re-read the plan/sizing object
    already held on the passed-in context.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C10
"""
from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path
from typing import Any

from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

_SOURCE_MEMO_RE = re.compile(r"^source_memo:\s*(.+?)\s*$", re.MULTILINE)

#: Frontmatter block delimiter — `---` alone on its own line, opening and
#: closing the YAML block. Used to scope `_SOURCE_MEMO_RE` to genuine
#: frontmatter rather than the whole file body.
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)


def _frontmatter_block(text: str) -> str:
    """The text between the leading `---` markers, or `""` if *text* does
    not open with a frontmatter block.

    Review: code-reviewer — `_SOURCE_MEMO_RE` was being applied to the
    entire plan file text, so a plan whose BODY prose happens to contain a
    line literally starting with `source_memo:` (e.g. quoting another
    plan's frontmatter) would be picked up as if it were real frontmatter.
    Scoping the search to this block closes that over-broad match surface."""
    match = _FRONTMATTER_BLOCK_RE.match(text)
    if not match:
        return ""
    return match.group(1)


def _today_dated_plan_collision(repo_root: Path) -> dict[str, Any]:
    """Leg (a): does another plan file dated today already exist under
    `docs/plans/`, or does today's git history already carry a commit
    touching `docs/plans/`.

    Returns a dict of raw evidence — never a verdict. `undetermined(...)`
    only if `docs/plans/` itself cannot be listed at all.
    """
    plans_dir = repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return undetermined(reason="docs/plans/ is not a directory under repo_root")

    today = datetime.date.today().isoformat()
    today_dated_files = sorted(
        p.name for p in plans_dir.glob(f"{today}*") if p.is_file()
    )

    commit_lines: list[str] = []
    try:
        proc = subprocess.run(
            [
                "git",
                "log",
                "--since=today",
                "--oneline",
                "--",
                "docs/plans/",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0:
            commit_lines = [
                line for line in proc.stdout.splitlines() if line.strip()
            ]
    except (OSError, subprocess.SubprocessError):
        commit_lines = []

    return {
        "today_dated_plan_files": today_dated_files,
        "today_dated_plan_hit": bool(today_dated_files),
        "today_commit_lines": commit_lines,
        "today_commit_hit": bool(commit_lines),
    }


def _source_memo_collision(
    repo_root: Path, source_memo: str | None, own_plan_path: Path | None
) -> dict[str, Any]:
    """Leg (b): does any OTHER plan file already carry a `source_memo:`
    frontmatter key naming *source_memo*, with a body-mention fallback.

    Returns `undetermined(...)` if `source_memo` was not supplied (this
    plan is not actioning a cross-repo memo, so the leg does not apply) or
    if `docs/plans/` cannot be listed.
    """
    if not source_memo:
        return undetermined(
            reason="plan frontmatter carries no source_memo: — leg (b) does not apply"
        )

    plans_dir = repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return undetermined(reason="docs/plans/ is not a directory under repo_root")

    memo_basename = Path(source_memo).name
    frontmatter_hits: list[str] = []
    body_mention_hits: list[str] = []

    for plan_file in sorted(plans_dir.glob("*.md")):
        if own_plan_path is not None and plan_file.resolve() == own_plan_path.resolve():
            continue
        try:
            text = plan_file.read_text(encoding="utf-8")
        except OSError:
            continue

        match = _SOURCE_MEMO_RE.search(_frontmatter_block(text))
        if match and memo_basename in match.group(1):
            frontmatter_hits.append(plan_file.name)
        elif memo_basename in text:
            body_mention_hits.append(plan_file.name)

    return {
        "memo_basename": memo_basename,
        "frontmatter_hits": frontmatter_hits,
        "frontmatter_hit": bool(frontmatter_hits),
        "body_mention_hits": body_mention_hits,
        "body_mention_hit": bool(body_mention_hits),
    }


def concurrent_preflight(context: PredicateContext) -> dict[str, Any]:
    """Compute `gates.substrate.concurrent_preflight` for row `:83`.

    Emits both legs' raw evidence as sibling keys — `today_dated_plan`
    (leg a) and `source_memo_collision` (leg b) — never a combined
    verdict. See module docstring for the KNOWN RESIDUAL leg (b) does not
    cover.
    """
    source_memo = None
    if context.plan_frontmatter is not None:
        raw = context.plan_frontmatter.get("source_memo")
        if isinstance(raw, str) and raw:
            source_memo = raw

    return {
        "today_dated_plan": _today_dated_plan_collision(context.repo_root),
        "source_memo_collision": _source_memo_collision(
            context.repo_root, source_memo, context.plan_path
        ),
    }


__all__ = ["concurrent_preflight"]
