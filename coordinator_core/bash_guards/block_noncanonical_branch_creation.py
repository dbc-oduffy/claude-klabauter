"""coordinator_core.bash_guards.block_noncanonical_branch_creation --
PreToolUse(Bash) advisory guard closing the branch-creation-seam gap: a
Bash-typed `git checkout -b`/`git checkout -B`/`git switch -c`/`git switch
-C`/`git branch <name>` invocation whose target name is neither the
canonical daily-branch shape (`work/{machine}/{today}` or a same-shape
lowercase span, per `coordinator_core.daily_branch.is_canonical_branch`) nor
one of a small sanctioned set of longlived prefixes is flagged via an
advisory `additionalContext` (never denied -- see
`docs/plans/2026-08-06-apply-guard-class-census.md`, chunk C13/C14: this
guard moved CONFINEMENT_DENY -> ADVISORY_REWRITE, `fail_closed=False`).

Why this exists: `daily_branch.py`'s oracles are pure predicates with no
enforcement seam of their own -- something has to call `is_canonical_branch`
at the point a branch actually gets created, or the oracle is advisory only.
A stray `git checkout -b fix/foo` or `git branch closeout-notes` creates a
ref this fleet's daily-branch discipline never intended, and once created it
persists (branches are cheap to make, easy to forget). This guard is that
enforcement seam, scoped to the repos where the concurrent-session/Windows
case-collision hazard is real (see "REPO SCOPING" below) -- not a fleet-wide
ban.

Structural template: `block_worktree_creation.py` (same seam -- PreToolUse
Bash hard-deny, same envelope shape, same NO-IDENTITY-GATE posture: this
guard fires on every caller including the main-loop EM, no
`agent_id`/`agent_type` exemption). Deliberate contrast with THAT guard,
which is a fleet-wide ban on purpose: this guard's ban is NOT fleet-wide --
see "REPO SCOPING" below. Do not conclude from the worktree guard's
unscoped posture that scoping is optional here; it is the opposite lesson.

WHAT THIS DENIES -- creation forms only, discriminated by FLAG PRESENCE, not
subcommand name:
  `git checkout -b <name>` / `git checkout -B <name>`
  `git switch -c <name>` / `git switch -C <name>` / `git switch --create
  <name>` / `git switch --force-create <name>`
  `git branch <name>` / `git branch <name> <start-point>`
A `git branch` invocation carrying ANY flag other than the small, explicit
create-compatible allowlist (`_BRANCH_CREATE_COMPATIBLE_FLAGS`: `-f`/
`--force`, `-t`/`--track`/`--no-track` -- flags that can accompany a real
creation without changing what it does) is NOT treated as a creation form
and is left untouched -- this is an ALLOWLIST, not the enumerated
short-flag blocklist an earlier revision used (Review: coordinator:code-
reviewer P1, Finding 2 -- `-d`/`-D`/`-m`/`-M`/`--list`/`-l` alone missed
git's long-form spellings `--delete`/`--move`/`--copy`/`-c`/`-C` entirely,
misclassifying `git branch --delete stray-fix-branch` as a creation of
`stray-fix-branch` and denying a delete). Treating an UNRECOGNIZED
`git branch` flag as non-create (fail open) is the deliberately safer
direction: `is_canonical_branch` has no opinion on renames/deletes/lists,
and denying one of those is the single sharpest recorded do-not-re-import
in this corpus -- the retired bash hook this guard's lineage traces to
covered rename too, and that is exactly what made mid-span rename
collisions a hazard the fleet had to work around. Discriminating by flag
presence (not by inspecting which subcommand form was used) is what keeps
`git branch -m old new` / `git branch --delete old` safe even though both
lexically match "`git branch <name>`".

Target-name extraction goes through
`coordinator_core.bash_guards._command_tokenizer.resolve_command_positions`
(quote-aware, `;`/`&&`/`|`-segmented, env-assignment- and
wrapper-binary-peeling) -- never a first-segment-only parser -- so a
compound (`cmd && git checkout -b x`) or env-prefixed (`FOO=1 git checkout
-b x`) invocation is classified identically to the bare form.

THE CANONICAL-SHAPE PREDICATE. Keys off `is_canonical_branch`, never the
looser `is_allowed_branch` -- the latter accepts mixed case by design (it
exists so a REMEDIATION path can recognise "valid shape, wrong case" and
offer a rename), which would let `work/MACHINE-B/2026-07-13` sail through
at CREATION time and reintroduce the Windows case-insensitive-ref collision
hazard `is_canonical_branch` exists to close.

    SANCTIONED_LONGLIVED_PREFIXES = ("migration/", "release/", "feature/")

A name under one of these prefixes is NOT denied here -- it is not a daily
branch and `is_canonical_branch` correctly says no, but a sanctioned
longlived branch is a legitimate, deliberately-named exception, not a
mistake to block. (A separate advisory guard owns nudging these toward
sane naming; this module only exports the prefix tuple for that guard to
import -- it does not implement the advisory itself.)

FAIL OPEN ON A NAME THIS GUARD NEVER ACTUALLY SAW. Before running the
canonical-shape predicate, a target name that is empty (after stripping
whitespace), starts with `$`, or contains a backtick or `$(` is treated as
"not a deny" -- a normal predicate leg, not an exception handler. This
covers both the literal-unexpanded-variable case (`git checkout -b
"$BRANCH"` tokenizes to the literal 4-character string `$BRANCH`, which is
plainly not a real branch name this guard can evaluate) and the
command-substitution case where the substitution IS the whole name: `git
checkout -b "$(date +%F)"` classifies as target name `" "` (a lone space),
which the "empty after stripping" leg catches.

A PARTIAL substitution -- the substitution is only part of the literal,
e.g. `git checkout -b "work/machine-b/$(date +%F)"` -- is a DIFFERENT
shape the "whole name" checks above do not reach (Review: coordinator:
code-reviewer P1, Finding 1, confirmed empirically against the real
`resolve_command_positions`, not reasoned from this docstring):
`_extract_command_substitutions` neutralizes the recognized `$(...)`/
backtick span to a single space *before* tokenization ever runs, so by the
time a name reaches `_looks_unsafe` the substitution marker itself is
already gone -- neither `resolved.tokens` nor `resolved.raw_tokens` retain
a `$(`/backtick span at the substitution's original position (both are
derived from the SAME post-neutralization text), so there is no
structural signal left to key off. What survives instead is a
neutralization ARTIFACT in the extracted name's shape: the quoted form
(`"work/machine-b/$(date +%F)"`) yields `"work/machine-b/ "` (an internal/
trailing literal space -- `name != name.strip()`); the unquoted form
(`work/machine-b/$(date +%F)`) yields `"work/machine-b/"` (shlex splits at
the neutralizing space, truncating to a trailing `/`). `_looks_unsafe`
therefore also fails open on a name that differs from its own
`.strip()`'d form, or that starts/ends with `/` or contains `//` -- shapes
no legitimate git ref (canonical or longlived-prefixed) can ever have, so
this can only ever ADD fail-open coverage, never suppress a real deny.

Denying on any of these shapes would be denying a decision this guard
cannot actually evaluate, which is worse than staying silent -- the same
"fail open on maximal uncertainty, never manufacture a deny from a token
you cannot read" posture the shared tokenizer's own `UNRESOLVED` fallback
uses on unparseable input (a single `[cmd_text]`-shaped entry, which
structurally cannot match any of this guard's two-or-more-token creation
shapes either, so an unterminated-quote command passes through the same
way).

NEGATIVE SPEC -- `ResolvedCommand.confidence` is never read. Its own
docstring forbids wiring it to a guard verdict (it classifies the segment
HEAD token, e.g. "git", not an argument -- `git checkout -b "$BRANCH"`
comes back RESOLVED with `$BRANCH` sitting in `tokens`, so `confidence`
would tell this guard nothing useful about the argument it actually needs
to judge).

REPO SCOPING -- deliberate, and unlike the sibling worktree ban this is NOT
optional. Resolved via `resolve_git_root(payload cwd)` then gated on
`_is_hazard_repo(git_root)` (both imported, never re-implemented): if the
repo is not one this machine's fleet registry tracks (nor the `~/.claude`
meta-repo), `check()` returns allow BEFORE any name predicate runs. An
ordinary OSS-consumer clone has no daily-branch discipline to enforce and
no concurrent-multi-EM traffic creating the collision hazard this predicate
exists for -- `git checkout -b fix/foo` in a foreign repo must never be
denied. `_is_hazard_repo` already fails OPEN internally (any classification
error -> "not a hazard repo") and needs no additional try/except here.

NEGATIVE SPEC 2 -- no date/staleness comparison anywhere in this module.
`is_canonical_branch` is a pure SHAPE predicate; whether a branch's date
segment is stale is a completely different concern (`should_prompt_rename`,
elsewhere) that this creation-time guard has no business computing.
`coordinator_core.daily_day.local_day` is imported ONLY to render today's
date into the deny message's remediation text, never to compare against
the attempted branch name.

NEGATIVE SPEC 3 -- no override env var (PM ruling R4). This guard does not
append `operator_override_note`, and does not name
`COORDINATOR_OVERRIDE_BRANCH` anywhere, including in prose explaining that
it does not work: `_alternative_liveness._OVERRIDE_RE` would classify any
such mention as an offered OVERRIDE alternative and grade it DEAD (no such
variable is ever read), failing the liveness gate over a sentence that
existed only to say "there is no bypass."

NEGATIVE SPEC 4 -- no try/except in `check()`. Exception-routing policy
(crash-deny vs crash-swallow-as-allow) is the dispatcher's job, driven by
the registration's `fail_closed` flag, not this module's; catching and
silently allowing here would duplicate that contract instead of deferring
to it. Since chunk C13/C14 (see summary above) this guard's registration
carries `fail_closed=False`, so a `check()` crash now routes to a swallowed
allow rather than a deny -- a deliberate consequence of the advisory flip,
not a reason to add a try/except here.

Spec: docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C1.
Advisory flip: docs/plans/2026-08-06-apply-guard-class-census.md, C13/C14.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._helpers import resolve_git_root
from coordinator_core.bash_guards.dispatch_checks import _is_hazard_repo
from coordinator_core.daily_branch import is_canonical_branch
from coordinator_core.daily_day import local_day

CLASS = "hard-deny"
MATCHERS = ["Bash"]
PRIORITY = 42

#: Longlived branch prefixes this guard deliberately does NOT deny -- see
#: module docstring "THE CANONICAL-SHAPE PREDICATE". Exported for
#: `guard_longlived_branch_naming.py` (a sibling chunk's advisory guard) to
#: import rather than re-declaring.
SANCTIONED_LONGLIVED_PREFIXES = ("migration/", "release/", "feature/")

#: Cheap pre-filter -- a candidate creation-shaped invocation must at least
#: mention one of these subcommand words; gates whether the more expensive
#: tokenized pass below runs at all.
_PRE_FILTER_RE = re.compile(r"\b(checkout|switch|branch)\b")

#: ALLOWLIST of `git branch` flags that may accompany a genuine CREATION
#: invocation without changing its classification -- `-f`/`--force`
#: (overwrite-if-exists) and `-t`/`--track`/`--no-track` (upstream-tracking
#: mode set at creation time). ANY OTHER flag (rename, delete, copy, list,
#: or anything this project has not audited) routes to non-create/allow --
#: see module docstring "WHAT THIS DENIES". Deliberately an allowlist, not
#: a blocklist of known-non-create flags: a blocklist misses git's
#: long-form spellings (`--delete`, `--move`, `--copy`) and fails toward
#: "creation, deny" on any flag it hasn't enumerated, which is the wrong
#: direction -- Review: coordinator:code-reviewer P1, Finding 2.
_BRANCH_CREATE_COMPATIBLE_FLAGS = frozenset({"-f", "--force", "-t", "--track", "--no-track"})

#: `git checkout`/`git switch` creation flags -- the token immediately
#: following one of these is the target branch name. `git switch` also
#: accepts the long-form `--create`/`--force-create` spellings of `-c`/
#: `-C` (Review: coordinator:code-reviewer P1, Finding 3 -- these were
#: absent, so `git switch --create <name>` bypassed the guard entirely).
_CHECKOUT_CREATE_FLAGS = frozenset({"-b", "-B"})
_SWITCH_CREATE_FLAGS = frozenset({"-c", "-C", "--create", "--force-create"})


def _looks_unsafe(name: str) -> bool:
    """True if `name` is not a real, readable branch-name literal this
    guard can evaluate -- see module docstring "FAIL OPEN ON A NAME THIS
    GUARD NEVER ACTUALLY SAW".

    The whitespace-artifact and slash-shape checks below catch a PARTIAL
    command substitution (the substitution is only part of the literal,
    e.g. `"work/machine-b/$(date +%F)"`) -- neither `resolved.tokens` nor
    `resolved.raw_tokens` retain a structural marker of the substitution by
    the time a name reaches here (`_extract_command_substitutions`
    neutralizes it to a space before tokenization), so this guard keys off
    the neutralization ARTIFACT left in the name's shape instead: an
    internal/trailing literal space (`name != name.strip()`, the quoted
    form) or a leading/trailing/doubled `/` (the unquoted form, where shlex
    splits at the neutralizing space and truncates to a trailing `/`). No
    legitimate git ref -- canonical or longlived-prefixed -- can ever have
    either shape, so this can only ever ADD fail-open coverage, never
    suppress a real deny. Confirmed empirically against the real
    `resolve_command_positions` for both the quoted and unquoted forms
    (Review: coordinator:code-reviewer P1, Finding 1)."""
    if not name or not name.strip():
        return True
    if name != name.strip():
        return True
    if name.startswith("$"):
        return True
    if "`" in name or "$(" in name:
        return True
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return True
    return False


def _deny(name: str) -> bool:
    """The canonical-shape predicate. See module docstring "THE
    CANONICAL-SHAPE PREDICATE"."""
    if is_canonical_branch(name):
        return False
    if name.startswith(SANCTIONED_LONGLIVED_PREFIXES):
        return False
    return True


def _extract_checkout_switch_target(
    tokens: List[str], create_flags: frozenset
) -> Optional[str]:
    """Return the token immediately following the first `create_flags`
    member found in `tokens[2:]` (the argv slice after `git <subcommand>`),
    or `None` if no such flag is present or it carries no following
    argument."""
    for i in range(2, len(tokens)):
        if tokens[i] in create_flags:
            return tokens[i + 1] if i + 1 < len(tokens) else None
    return None


def _extract_branch_target(tokens: List[str]) -> Optional[str]:
    """Return the target name for a `git branch ...` invocation, or `None`
    if this is not a creation form. See module docstring "WHAT THIS
    DENIES". Any flag NOT in `_BRANCH_CREATE_COMPATIBLE_FLAGS` (rename,
    delete, copy, list, or anything else this project has not audited)
    routes to `None` (non-create, allow) -- an allowlist so an
    unrecognized flag fails toward "not creation" rather than toward
    "creation, deny"."""
    args = tokens[2:]
    if any(tok.startswith("-") and tok not in _BRANCH_CREATE_COMPATIBLE_FLAGS for tok in args):
        return None
    positional = [tok for tok in args if not tok.startswith("-")]
    if not positional:
        return None
    return positional[0]


def _classify_segment(tokens: List[str]) -> Optional[str]:
    """Return the offending (denied) branch name for one resolved
    command-position segment's `tokens`, or `None` (allow -- not a
    creation-shaped git invocation, or the target name is safe/unsafe to
    evaluate per the predicates above)."""
    if len(tokens) < 2:
        return None
    if not token_matches_binary(tokens[0], "git"):
        return None

    subcmd = tokens[1]
    if subcmd == "checkout":
        name = _extract_checkout_switch_target(tokens, _CHECKOUT_CREATE_FLAGS)
    elif subcmd == "switch":
        name = _extract_checkout_switch_target(tokens, _SWITCH_CREATE_FLAGS)
    elif subcmd == "branch":
        name = _extract_branch_target(tokens)
    else:
        return None

    if name is None or _looks_unsafe(name):
        return None
    if not _deny(name):
        return None
    return name


def _canonical_example(today: str) -> str:
    """Resolve today's concrete canonical branch name for the deny
    message's remediation text -- e.g. `work/machine-b/2026-08-01` -- via
    `coordinator_core.machine_resolver.compute_machine` (the same live
    resolver `workday-start-day-branch-resolve.py` uses to build
    `work/{machine}/{date}`). Degrades to the `work/<machine>/{today}`
    template if the machine component cannot be resolved for any reason.

    Called ONLY from `_advisory_reason`, after `_deny`'s fire/no-fire verdict
    is already decided -- this never influences whether the guard denies
    (see module docstring "NEGATIVE SPEC 2")."""
    try:
        from coordinator_core.machine_resolver import compute_machine

        machine = compute_machine()
    except Exception:  # noqa: BLE001 -- message-text-only, fail open to the template
        machine = None
    if not machine:
        return f"work/<machine>/{today}"
    return f"work/{machine}/{today}"


def _advisory_reason(cmd: str, name: str) -> str:
    cmd_safe = cmd if len(cmd) <= 200 else cmd[:200] + "..."
    today = local_day()
    canonical = _canonical_example(today)
    return (
        "Advisory: `%s` is not a canonical branch name -- daily-branch "
        "discipline on this repo requires `work/{machine}/{date}` (or a "
        "sanctioned longlived prefix: `migration/`, `release/`, "
        "`feature/`).\n\n"
        "  Command:  %s\n\n"
        "Use instead: create or switch to your canonical daily branch: "
        "`git checkout -b %s`, or use a sanctioned longlived prefix if this "
        "is genuinely longlived work.\n\n"
        "If this really needs a differently-shaped branch name, that is a "
        "policy question for the PM/EM, not something a Bash call can "
        "grant itself."
    ) % (name, cmd_safe, canonical)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the noncanonical-branch-creation gate against a PreToolUse
    payload. Returns `None` (allow, no comment) or the nested advisory
    `allow` envelope (`additionalContext`, never `permissionDecision:
    "deny"` -- see module summary). Never identity-gated -- fires for every
    caller including the main-loop EM (see module docstring).
    """
    # Deliberately no try/except -- fail-CLOSED-on-exception is the
    # dispatcher's job for a CLASS = "hard-deny" guard (see module
    # docstring "NEGATIVE SPEC 4").
    if (payload.get("tool_name") or "") != "Bash":
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    if not _PRE_FILTER_RE.search(cmd):
        return None

    # REPO SCOPING -- see module docstring "REPO SCOPING". Must run before
    # any name predicate; an out-of-scope repo allows unconditionally.
    git_root = resolve_git_root(payload.get("cwd"))
    if not _is_hazard_repo(git_root or ""):
        return None

    for resolved in resolve_command_positions(cmd):
        name = _classify_segment(resolved.tokens)
        if name is not None:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": _advisory_reason(cmd, name),
                }
            }

    return None
