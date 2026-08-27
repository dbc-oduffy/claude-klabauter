"""coordinator_core.bash_guards.guard_longlived_branch_naming -- PreToolUse
(Bash) ADVISORY guard nudging away from speculative long-lived-shaped branch
creation, on the sanctioned-prefix band `block_noncanonical_branch_creation`
(chunk C1) deliberately does NOT deny.

Why this exists (ruling R4 carve-out): `docs/wiki/daily-branch-discipline.md`
names a SECOND legitimate, PM-authorized workstream shape beyond the
canonical dated cut -- `migration/*`, `release/*`, `feature/<name>` --
historically reached via a now-closed inline-override hatch. A bare
`is_canonical_branch` deny would foreclose that shape entirely, which is
exactly what would make an agent-invocable override hatch necessary again --
and R4 ruled there is no such hatch. This advisory band is the resolution:
canonical passes silently (never even matched here), sanctioned-longlived
passes WITH an advisory naming the speculative-topic-branch anti-pattern and
a concrete alternative, everything else is C1's deny. That three-band split
is what lets the no-hatch ruling survive without foreclosing genuinely
longlived, PM-authorized work.

WHAT THIS DOES -- and does not do:
  - Matches ONLY `git checkout -b/-B <name>` and `git switch
    -c/-C/--create/--force-create <name>` where `<name>` starts with one of
    `SANCTIONED_LONGLIVED_PREFIXES` (imported from
    `block_noncanonical_branch_creation`, never redefined).
  - NEVER denies, under any input shape, including malformed or empty
    names -- CLASS is `"advisory"`, not `"hard-deny"`; the only envelope
    this module ever returns is `None` (no-op) or
    `_hook_envelope.allow_advisory(...)`.
  - Canonical `work/*` names and `main` never even enter the prefix
    predicate -- the matcher itself is scoped to the three sanctioned
    prefixes, so this guard structurally cannot fire on canonical shape.
  - `git branch -m`/`-M` (rename) is untouched -- this guard, like its
    sibling C1 deny, only looks at `checkout -b/-B` and `switch -c/-C`
    creation forms; it does not even inspect `git branch`.

Structural template: `block_illegal_filename.py`'s Bash arm -- whole body in
one `try/except Exception: return None` (fail-OPEN; an advisory guard must
never crash a caller's Bash call over a naming nudge), envelope via
`_hook_envelope.allow_advisory("PreToolUse", ctx)`, never a bare exit-0
stdout write.

REPO SCOPING (AC13) -- this band inherits the foreign-repo over-firing risk
MORE acutely than C1's deny does: `feature/*` is arguably the single most
common branch prefix in the wider git world, so an unscoped advisory would
fire constantly on ordinary OSS-consumer clones with no daily-branch
discipline to nudge toward. Resolved via `resolve_git_root(payload cwd)`
then gated on `_is_hazard_repo(git_root)` (both imported, never
re-implemented) BEFORE the prefix predicate runs, mirroring C1's own
sequencing. `_is_hazard_repo` already fails OPEN internally (any
classification error -> "not a hazard repo") and needs no additional
try/except of its own.

NEGATIVE SPEC -- no override env var (PM ruling R4, same as C1). This module
does not name `COORDINATOR_OVERRIDE_BRANCH` or any
`COORDINATOR_(ALLOW|OVERRIDE|DISABLE)_*` token anywhere, including in prose
explaining that no such hatch exists: `_alternative_liveness._OVERRIDE_RE`
would classify any such mention as an offered OVERRIDE alternative and grade
it DEAD (no such variable is ever read), failing the liveness gate over a
sentence that existed only to say "there is no bypass."

NEGATIVE SPEC 2 -- the offered alternative never names another branch's
uncommitted state (stash contents, WIP diffs) as a "concrete alternative" --
per ruling R1/R2, an alternative must be concrete and applicable, and
uncommitted state living on a branch is not a git concept this guard (or any
guard) can assert liveness of. The anti-pattern named here is the
speculative `feature/<topic>-<date>` naming and checkout-stash-checkback
cycle itself, not any particular branch's contents.

Target-name extraction reuses the same
`coordinator_core.bash_guards._command_tokenizer.resolve_command_positions`
pipeline C1 uses (quote-aware, `;`/`&&`/`|`-segmented, env-assignment- and
wrapper-binary-peeling) -- never a first-segment-only parser.

Flag vocabulary: `switch` creation is matched via both short (`-c`/`-C`)
and git's own documented long-form aliases (`--create`/`--force-create`) --
closing the same closed-short-flag-vs-open-long-form gap a peer review
found on the deny side (`block_noncanonical_branch_creation.py`), so a
hand-typed `git switch --create feature/x` doesn't silently bypass this
advisory the way a short-flag-only match would let it.

NEGATIVE SPEC 3 -- no firing-rate bound, deliberately: unlike sibling C5
(`guard_branch_set_precedence.py`), which ships a two-leg recency filter
specifically to bound how often its advisory nags, this module fires on
EVERY sanctioned-prefix creation in a hazard repo, every time, for the life
of a session -- no once-per-session dedup, no recency window. This is a
known, deliberate gap, not an oversight: the plan's own C5 firing-rate
analysis was never extended here because whether repeated `feature/*`-style
creations in one session actually produce nag fatigue is an observed-rate
question, not something decidable from the code alone. Left for a future
chunk if warranted by observed firing rate -- mirrors the language the plan
already applies to C5's own firing-rate resolution.

Spec: docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C7.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from coordinator_core._hook_envelope import allow_advisory
from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    expand_start_process_invocations,
    tokenize_command,
)
from coordinator_core.bash_guards._helpers import resolve_git_root
from coordinator_core.bash_guards.block_noncanonical_branch_creation import (
    SANCTIONED_LONGLIVED_PREFIXES,
)
from coordinator_core.bash_guards.dispatch_checks import _is_hazard_repo
from coordinator_core.daily_day import local_day
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 43

#: Cheap pre-filter -- a candidate creation-shaped invocation must at least
#: mention one of these subcommand words; gates whether the more expensive
#: tokenized pass below runs at all.
_PRE_FILTER_RE = re.compile(r"\b(checkout|switch)\b")

#: `git checkout`/`git switch` creation flags -- the token immediately
#: following one of these is the target branch name. Mirrors C1's own
#: constants exactly (this guard does not inspect `git branch` at all).
_CHECKOUT_CREATE_FLAGS = frozenset({"-b", "-B"})
_SWITCH_CREATE_FLAGS = frozenset({"-c", "-C", "--create", "--force-create"})


def _looks_unsafe(name: str) -> bool:
    """True if `name` is not a real, readable branch-name literal this
    guard can evaluate -- same posture as C1's own leg of the same name:
    an unexpanded shell variable or command-substitution residue is not a
    decision this guard can evaluate, so it is treated as "not a match",
    never coerced into a false advisory."""
    if not name or not name.strip():
        return True
    if name.startswith("$"):
        return True
    if "`" in name or "$(" in name:
        return True
    return False


def _extract_create_target(tokens: List[str], create_flags: frozenset) -> Optional[str]:
    """Return the token immediately following the first `create_flags`
    member found in `tokens[2:]` (the argv slice after `git <subcommand>`),
    or `None` if no such flag is present or it carries no following
    argument."""
    for i in range(2, len(tokens)):
        if tokens[i] in create_flags:
            return tokens[i + 1] if i + 1 < len(tokens) else None
    return None


def _classify_segment(tokens: List[str]) -> Optional[str]:
    """Return the sanctioned-longlived branch name for one resolved
    command-position segment's `tokens`, or `None` (no advisory -- not a
    checkout -b/-B or switch -c/-C/--create/--force-create creation form,
    or the target name does not start with a sanctioned longlived prefix,
    or is unsafe to read)."""
    if len(tokens) < 2:
        return None
    if not token_matches_binary(tokens[0], "git"):
        return None

    subcmd = tokens[1]
    if subcmd == "checkout":
        name = _extract_create_target(tokens, _CHECKOUT_CREATE_FLAGS)
    elif subcmd == "switch":
        name = _extract_create_target(tokens, _SWITCH_CREATE_FLAGS)
    else:
        return None

    if name is None or _looks_unsafe(name):
        return None
    if not name.startswith(SANCTIONED_LONGLIVED_PREFIXES):
        return None
    return name


def _canonical_example(today: str) -> str:
    """Resolve today's concrete canonical branch name for the advisory
    message's remediation text -- mirrors
    `block_noncanonical_branch_creation._canonical_example` exactly (same
    live resolver, same message-text-only usage, same graceful degrade to
    the `work/<machine>/{today}` template). Called ONLY from
    `_advisory_ctx`, after the match verdict is already decided -- never
    influences whether this guard fires (this guard never denies at all,
    but the same "message-text-only" discipline applies here as it does to
    C1's deny -- see module docstring "NEGATIVE SPEC 2")."""
    try:
        from coordinator_core.machine_resolver import compute_machine

        machine = compute_machine()
    except Exception:  # noqa: BLE001 -- message-text-only, fail open to the template
        machine = None
    if not machine:
        return f"work/<machine>/{today}"
    return f"work/{machine}/{today}"


def _advisory_ctx(name: str) -> str:
    prefix = next(p for p in SANCTIONED_LONGLIVED_PREFIXES if name.startswith(p))
    canonical = _canonical_example(local_day())
    return (
        "LONGLIVED-BRANCH-NAMING ADVISORY (non-blocking): sanctioned `%s` "
        "allowed, but not for a scratch topic that never merges. "
        "Use instead: `git checkout -b %s`."
    ) % (prefix, canonical)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the longlived-branch-naming advisory gate against a
    PreToolUse payload. Returns `None` (no-op) or an allow+additionalContext
    advisory envelope -- NEVER a deny, under any input (see module
    docstring "WHAT THIS DOES").
    """
    try:
        if (payload.get("tool_name") or "") not in MATCHERS:
            return None

        tool_input = payload.get("tool_input") or {}
        cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
        if not cmd:
            return None
        cmd = cmd.replace("\r", "")

        if not _PRE_FILTER_RE.search(cmd):
            return None

        # REPO SCOPING (AC13) -- see module docstring. Must run before any
        # name predicate; an out-of-scope repo allows unconditionally.
        git_root = resolve_git_root(payload.get("cwd"))
        if not _is_hazard_repo(git_root or ""):
            return None

        # Dialect-aware Start-Process expansion (C8,
        # pln-the-destructive-core-learns-the-she): this entry's
        # `matchers` already declares `COMMAND_TOOL_NAMES` but
        # `resolve_command_positions` is a Bash-shaped tokenizer with no
        # PowerShell awareness, so a `Start-Process git -ArgumentList
        # 'checkout','-b','feature/x'` invocation resolves to a segment
        # headed by `Start-Process`, never `git`, and `_classify_segment`
        # below never reaches the sanctioned-prefix predicate -- even
        # though the base `git checkout -b` argv is byte-identical across
        # dialects. This guard NEVER denies (see module docstring "WHAT
        # THIS DOES"), so this is a missed advisory, never a spurious one.
        # Same narrow fix as the sibling entries: for a PowerShell payload
        # only, tokenize via `_dialect.tokenize_command` and run the SAME
        # `expand_start_process_invocations` pass, then rejoin the
        # expanded tokens back into text so `resolve_command_positions`
        # (unchanged, still exercised byte-for-byte on the BASH leg) sees
        # the target's real argv in command position. A PowerShell parse
        # failure leaves `cmd` untouched.
        _lln_dialect = dialect_from_tool_name(payload.get("tool_name"))
        if _lln_dialect is Dialect.POWERSHELL:
            _lln_ps_tokens = tokenize_command(
                cmd, _lln_dialect, guard_name="longlived-branch-naming"
            )
            if _lln_ps_tokens is not None:
                cmd = " ".join(expand_start_process_invocations(_lln_ps_tokens))

        for resolved in resolve_command_positions(cmd):
            name = _classify_segment(resolved.tokens)
            if name is not None:
                return allow_advisory("PreToolUse", _advisory_ctx(name))

        return None
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard is advisory-only
        # and must never turn a naming nudge into a crashed Bash call.
        return None
