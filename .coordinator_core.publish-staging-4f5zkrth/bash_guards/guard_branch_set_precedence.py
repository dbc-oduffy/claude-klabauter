"""coordinator_core.bash_guards.guard_branch_set_precedence -- PreToolUse(Bash)
ADVISORY that notices, on a canonical `git checkout -b <new-daily>`, when
another canonical daily-branch already carries commits main hasn't seen --
and offers to RESUME that branch instead of standing up a fresh one.

Why this exists: the daily-branch discipline mints a fresh `work/{machine}/
{today}` branch every day, but nothing upstream of this guard notices when a
PRIOR canonical branch is still carrying unmerged work at the moment a new
one is cut -- that work simply sits there, easy to forget, until it goes
stale enough for `/workday-start` Step 0's rename/roll-forward flow to pick
it up. This guard is the earlier, gentler nudge: if a same-shape branch is
both unmerged AND recently touched, say so before a second branch is cut
next to it.

Posture: ADVISORY-ONLY, mirroring `block_illegal_filename.py`'s Bash arm --
the one true never-denies template this module follows structurally. This
guard NEVER denies, NEVER auto-stashes, and NEVER auto-commits; its whole
`check()` body is wrapped in `try/except Exception: return None` (fail-OPEN),
and its only caller-facing shape is `_hook_envelope.allow_advisory`.

Ordering (plan ruling R3): the command shape and the new branch's target
name are resolved with ZERO subprocesses first (tokenizer + pure-Python
shape predicates only). Only once that target name is confirmed to be a
real canonical daily-branch creation (`daily_branch.is_canonical_branch`)
does this module call into `_branch_set` (C3) -- which is itself gated on
`_is_hazard_repo` (AC13) before it ever runs C3's `for-each-ref` enumeration.
A non-matching command spends zero git subprocesses; a canonical-shaped
command spends exactly one (`resolve_git_root`, to resolve hazard status --
there is no way to know a repo is non-hazard without first resolving its
root), and a second (`_branch_set.ahead_of_main`) only once a real candidate
branch set is found and named. R3's actual requirement -- git calls gated
behind the zero-subprocess canonical-shape check, so spend is a few times a
day rather than per Bash call -- holds regardless.

Flag vocabulary (deliberate widening beyond the plan's literal C5 body,
which reads "on a canonical `git checkout -b <new-daily>`"): this module
also matches `git switch -c/-C/--create/--force-create <new-daily>`.
`git switch -c` is the idiomatic modern spelling (git >= 2.23) of the same
branch-creation operation `checkout -b` performs, and a peer C1/C7-adjacent
review found the identical closed-short-flag-vs-open-long-form gap on the
deny side -- an advisory whose entire purpose is to catch a second canonical
branch being cut is defeated if the single most common modern spelling of
"cut a branch" silently bypasses it. `--create`/`--force-create` are git's
own documented long-form aliases for `-c`/`-C`, not a guess.

The candidate filter (AC16) -- a candidate survives ONLY if:
  - it is canonical shape and not the branch being stood on (already
    guaranteed by `_branch_set.other_canonical_branches`, C3's own filter);
  - it carries commits not in main (also C3's `--no-merged=main` leg);
  - (a) its last commit is <= 48h old, per `daily_branch._HOURS_48_SECONDS`
    (imported, never re-derived -- this recency filter is deliberate and
    permitted: AC9's date-comparison ban scopes to C1's DENY only, and a
    recency filter on an ADVISORY makes it fire LESS, the opposite of the
    over-firing hazard that ban guards against);
  - (b) `daily_branch.should_prompt_rename(...)` is False for it -- when
    True, `/workday-start` Step 0's rename/roll-forward path already owns
    that branch, and offering "resume it" here would be the wrong remedy
    for an already-handled case (the Axis-A "concrete and APPLICABLE
    alternative" bar forbids offering a dead/inapplicable alternative).

Among survivors, the most-recently-touched (max commit epoch) is the one
named in the advisory -- deterministic given a fixed candidate list, and the
only branch `_branch_set.ahead_of_main` is ever called for (spent at most
once, never fanned out across every candidate -- see that module's own
cost-discipline docstring).

Ruling R2 -- the alternative offered is always RESUME (`git checkout
<branch>`), never stash: a non-checked-out branch's uncommitted state is not
a git concept this guard can reason about (ruling R1); stashing describes
the branch a caller is STANDING ON, which is a different gap than this one.

Injection seam: `branch_set_provider` -- an optional zero-arg callable
returning the same `List[Tuple[str, int]]` shape as `_branch_set.
other_canonical_branches`. Without it, the firing condition depends on the
live repo's branch state, which breaks on a fresh clone, in CI, or on any
day the tree happens to be merged clean; C2's alternative-liveness gate
drives this guard with a fixed synthetic branch set through this seam so it
fires deterministically. `_other_canonical_branches`/`_ahead_of_main`/
`resolve_git_root`/`_is_hazard_repo`/`_now`/`_today` are each imported as
this module's OWN attribute (never the upstream definition) so a test can
monkeypatch this module's copy per this package's injection convention.

Negative-spec:
  - Does NOT deny, stash, or commit under any circumstance -- CLASS is
    `"advisory"`.
  - Does NOT compare dates itself for the CREATION gate -- `is_canonical_
    branch` is a pure shape predicate; the only date/epoch comparisons in
    this module are the AC16 candidate-recency legs, which are explicitly
    permitted (see above).
  - Does NOT register itself anywhere -- `dispatch.py`/`_alternative_
    liveness.py` wiring is C2's remit, not this module's.
  - Does NOT re-derive `_HOURS_48_SECONDS`, `should_prompt_rename`, or
    `is_allowed_branch`/`is_canonical_branch` -- all imported from
    `daily_branch`.
  - Does NOT fan out `ahead_of_main` across every candidate -- called
    exactly once, for the single branch named in the advisory.

Spec: docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C5.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._branch_set import ahead_of_main as _ahead_of_main
from coordinator_core.bash_guards._branch_set import (
    other_canonical_branches as _other_canonical_branches,
)
from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._helpers import resolve_git_root
from coordinator_core.bash_guards.dispatch_checks import _is_hazard_repo
from coordinator_core.daily_branch import _HOURS_48_SECONDS, is_canonical_branch, should_prompt_rename
from coordinator_core.daily_day import local_day
from coordinator_core._hook_envelope import allow_advisory
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 100

#: `git checkout` creation flags -- the token immediately following one of
#: these is the new branch's target name. Mirrors `block_noncanonical_
#: branch_creation.py`'s own `_CHECKOUT_CREATE_FLAGS`, deliberately NOT
#: imported from that module -- this guard owns its own (much narrower)
#: shape resolution rather than coupling to a sibling chunk's private
#: extraction helpers.
_CHECKOUT_CREATE_FLAGS = frozenset({"-b", "-B"})

#: `git switch` creation flags -- short (`-c`/`-C`) and git's own documented
#: long-form aliases (`--create`/`--force-create`). See module docstring
#: "Flag vocabulary" for why `switch` is matched at all.
_SWITCH_CREATE_FLAGS = frozenset({"-c", "-C", "--create", "--force-create"})

#: Cheap pre-filter -- a candidate creation-shaped invocation must at least
#: mention one of these subcommand words; gates whether the more expensive
#: tokenized pass below runs at all. Word-boundary regex (aligned to C7's
#: `_PRE_FILTER_RE` shape) so e.g. `echo mycheckout` doesn't positively gate.
_PRE_FILTER_RE = re.compile(r"\b(checkout|switch)\b")

#: A `BranchSetProvider` returns the same `(name, committer_epoch)` pairs as
#: `_branch_set.other_canonical_branches`.
BranchSetProvider = Callable[[], List[Tuple[str, int]]]


def _now() -> float:
    """Module-level wall-clock seam -- tests monkeypatch this attribute
    directly for deterministic recency-filter behavior (never live-clock-
    dependent, per this chunk's test requirements)."""
    return time.time()


def _today() -> str:
    """Module-level "today" seam -- mirrors `_now()`; tests monkeypatch this
    attribute directly rather than the live `daily_day.local_day` clock."""
    return local_day()


def _looks_unsafe(name: str) -> bool:
    """True if `name` is not a real, readable branch-name literal this
    guard can evaluate (unexpanded variable, command substitution, or
    empty/whitespace-only) -- mirrors `block_noncanonical_branch_creation.
    _looks_unsafe`'s fail-open posture."""
    if not name or not name.strip():
        return True
    if name.startswith("$"):
        return True
    if "`" in name or "$(" in name:
        return True
    return False


def _extract_checkout_target(tokens: List[str]) -> Optional[str]:
    """Return the token immediately following the create flag in a `git
    checkout -b/-B ...` or `git switch -c/-C/--create/--force-create ...`
    segment's tokens, or `None` if this is not a creation form. Mirrors
    C7's `_classify_segment` subcommand dispatch -- see module docstring
    "Flag vocabulary"."""
    if len(tokens) < 2:
        return None
    if not token_matches_binary(tokens[0], "git"):
        return None
    subcmd = tokens[1]
    if subcmd == "checkout":
        create_flags = _CHECKOUT_CREATE_FLAGS
    elif subcmd == "switch":
        create_flags = _SWITCH_CREATE_FLAGS
    else:
        return None
    for i in range(2, len(tokens)):
        if tokens[i] in create_flags:
            return tokens[i + 1] if i + 1 < len(tokens) else None
    return None


def _find_new_daily_target(cmd: str) -> Optional[str]:
    """Zero-subprocess resolution of the new branch's target name from a
    `git checkout -b/-B <name>` or `git switch -c/-C/--create/--force-create
    <name>` segment, or `None` if no such creation-shaped segment is
    present, or the target is unreadable/not a canonical daily-branch name.
    Never calls into `_branch_set` -- see module docstring "Ordering"."""
    for resolved in resolve_command_positions(cmd):
        name = _extract_checkout_target(resolved.tokens)
        if name is None or _looks_unsafe(name):
            continue
        if is_canonical_branch(name):
            return name
    return None


def _survives_recency_filter(name: str, epoch: int, now: float, today: str) -> bool:
    """AC16's two-leg recency filter -- both legs must hold for a candidate
    to survive: (a) last-commit age <= 48h (`daily_branch._HOURS_48_
    SECONDS`), and (b) `should_prompt_rename` is False (a True verdict means
    `/workday-start` Step 0 already owns this branch; see module docstring).
    """
    age_seconds = now - epoch
    if age_seconds > _HOURS_48_SECONDS:
        return False
    if should_prompt_rename(name, today, epoch, now_epoch=now):
        return False
    return True


def _select_candidate(
    candidates: List[Tuple[str, int]], now: float, today: str
) -> Optional[Tuple[str, int]]:
    """Filter `candidates` per `_survives_recency_filter`, then return the
    most-recently-touched survivor (max commit epoch), or `None` if none
    survive."""
    survivors = [
        (name, epoch)
        for name, epoch in candidates
        if _survives_recency_filter(name, epoch, now, today)
    ]
    if not survivors:
        return None
    return max(survivors, key=lambda pair: pair[1])


def _offer_ctx(branch: str, ahead_count: int) -> str:
    """AC7's offer text -- names the real branch and real count, and always
    offers RESUME (ruling R2, never stash). Carries an `_alternative_
    liveness` cue token ("instead", the bare-word alternative -- this
    guard's text is "Resume it instead:", not the colon-suffixed "Use
    instead:" phrase C7 uses) over a backtick command span so extraction
    classifies it as a live `AlternativeKind.COMMAND`."""
    return (
        "%s already has %d unmerged commit%s. Resume it instead: "
        "`git checkout %s` -- or keep cutting this branch if that work "
        "is done."
    ) % (branch, ahead_count, "" if ahead_count == 1 else "s", branch)


def check(
    payload: Dict[str, Any], branch_set_provider: Optional[BranchSetProvider] = None
) -> Optional[Dict[str, Any]]:
    """Evaluate the branch-set-precedence advisory against a PreToolUse
    payload. Returns `None` (no advisory) or an `allow_advisory` envelope --
    never a deny, never any other shape. See module docstring for the full
    ordering/filter contract.
    """
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in MATCHERS:
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        cmd = tool_input.get("command") or ""
        if not cmd:
            return None
        cmd = cmd.replace("\r", "")

        if not _PRE_FILTER_RE.search(cmd):
            return None

        target = _find_new_daily_target(cmd)
        if target is None:
            return None

        # AC13 -- repo scoping BEFORE running C3's enumeration. Only reached
        # once the command is confirmed to be a real canonical daily-branch
        # creation (zero subprocesses spent above).
        cwd = payload.get("cwd")
        git_root = resolve_git_root(cwd)
        if not _is_hazard_repo(git_root or ""):
            return None

        if branch_set_provider is not None:
            candidates = branch_set_provider()
        else:
            candidates = _other_canonical_branches(cwd=cwd)
        if not candidates:
            return None

        now = _now()
        today = _today()
        selected = _select_candidate(candidates, now, today)
        if selected is None:
            return None

        branch, _epoch = selected
        ahead_count = _ahead_of_main(branch, cwd=cwd)
        if ahead_count <= 0:
            return None

        ctx = _offer_ctx(branch, ahead_count)
        return allow_advisory("PreToolUse", ctx)
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard is advisory-only
        # and must never wedge or alter the PreToolUse(Bash) hot path.
        return None
