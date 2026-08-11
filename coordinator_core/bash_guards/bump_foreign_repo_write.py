"""coordinator_core.bash_guards.bump_foreign_repo_write -- the Bash-surface
CROSS-REPO write-confinement speed bump (C4): a well-meaning session commits
or writes into a git repo other than its own, and nothing today notices.

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md [example-doctrine-repo
repo], chunk C4 "Cross-repo detection and registration".

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY. Read the plan's "Design
posture -- passable by construction" section before touching this module.
FAIL OPEN, EVERYWHERE: every unresolvable step (no anchor, no gitdir, no
registry, an unparseable command) returns `None` (no bump) -- the OPPOSITE
of this package's fail-closed neighbours. A bump that misfires on legitimate
work gets disabled, and a disabled guard prevents nothing.

Reuses rather than reimplements (see each import site for why):
  - `_command_tokenizer.resolve_command_positions` -- THE resolve-once
    entry point (wrapper/env peeling, quote-aware segmentation, heredoc
    stripping) this whole package already standardises on. This module does
    NOT re-tokenize or re-segment on its own.
  - `_write_bump_applicability` (C2) -- whether the bump applies at all, and
    whether an anchor with no git repo of its own still bumps against a
    REGISTERED target.
  - `_write_bump_marker` (C3) -- the session-scoped clear-once marker
    (`bump_is_cleared`), gitdir resolution (`resolve_gitdir`), and the
    subagent-inherits-EM's-marker back-pointer (`effective_session_id`).
  - `_write_bump_message` (C6) -- ALL user-facing copy. This module never
    composes its own text.
  - `_write_bump_sink_shapes` -- the plain-bash write-sink shape table this
    chunk owns and C5 (next wave) imports, never restates.
  - `block_reviewer_bash_outside_allowlist._GIT_READONLY_SUBCOMMANDS` -- the
    package's own existing read-only-git-subcommand allowlist, reused here
    (inverted: unlisted means "assume write", matching that module's own
    deny-by-omission convention) rather than a second, independently
    hand-maintained list of git verbs.
  - `commit_tripwires._same_tree`'s own `os.path.realpath`-equality shape
    for "same repo root" -- see `_same_repo_root` below for why this module
    ALSO case-folds both operands before calling it (Anti-scope; `_same_
    tree`'s own `os.path.normcase` is a no-op on POSIX and this fleet's
    primary filesystem, APFS, is case-insensitive).

WHAT COUNTS AS "THE SESSION'S OWN REPO" FOR THIS COMPARISON -- the session's
ANCHORED launch root (`_write_bump_applicability.resolve_launch_anchor`),
never the live payload `cwd`. This is the same anchor C2 uses for
applicability, reused here for the identical reason (AC12): the live payload
`cwd` drifts across Bash calls (harness contract: "Working directory
persists between calls"), so a `cd <other-repo> && git commit` -- exactly
the shape this guard exists to catch -- would, if compared against a
LIVE-cwd-derived "own repo", always find cwd already AT the target after the
`cd`, self-defeating the whole guard. Anchoring on the stable launch root is
what makes the comparison meaningful at all.

WHERE THE MARKER LIVES -- narrowed per-(session, TARGET), never per-session
(chunk C3, docs/plans/2026-08-03-narrow-write-confinement-bump.md, AC4).
`bump_is_cleared`/`clear_line`/`resolve_gitdir` are all called against
`probe_dir` -- the resolved TARGET, not the session's own anchor -- for
every candidate this guard bumps against, generalizing what used to be the
§ No-repo anchor branch's own fallback (`marker_probe = probe_dir`, kept
unchanged below) to the ordinary anchor-has-a-repo case too. `probe_dir` is
itself resolved off `cwd`-tracking within THIS command (never live payload
`cwd` drift across calls -- see "CWD-SENSITIVITY" below), so the printed
clear line and a later call's marker check still agree regardless of how
many `cd`s happen in between (AC6, AC12); what changed is WHICH gitdir that
agreement is keyed on, not the anchoring discipline itself. Clearing the
bump for target A therefore leaves it firing for target B in the same
session (AC4) -- a deliberate narrowing from the prior "one clear covers
every foreign target for the rest of the session" property, per this
plan's own Design table.

CWD-SENSITIVITY -- this guard's `check_bump_foreign_repo_write` receives the
raw payload `cwd` (in addition to `cmd`/`session_id`/`payload`) because a
`git -C <dir>`/`cd <dir> && git ...`/plain-bash-write-sink target is always
resolved RELATIVE to wherever the command actually runs, and that starting
point is the live payload cwd, not the anchor -- the anchor answers "what is
the session's own repo", `cwd` answers "where does THIS command's own
relative-path resolution begin". `dispatch.py` warns against widening `cwd`
to a shared, dispatcher-level `resolve_git_root()` call reused across every
check (the F0 hazard) -- this module does not do that: it resolves paths
itself, locally, exactly once per candidate, using `cwd` only as a starting
point for THIS guard's own relative-path walk, never caching a resolved
root for reuse by another guard.

REGISTRATION (`dispatch.py`'s `_build_guard_chain`) -- `fail_closed=False`,
the OPPOSITE of every neighbouring `CONFINEMENT_DENY` entry: a crash in this
guard must be swallowed as "allow", never routed through the hard-deny crash
path, because this guard's entire job is an advisory nudge, not a
confinement -- crashing closed here would turn a passable speed bump into an
accidental hard wall the moment this module has a bug. `band=GuardBand.
ADVISORY_REWRITE`, NOT `CONFINEMENT_DENY`: the blanket-disarm marker can
suppress every band except `CONFINEMENT_DENY` (`dispatch.py`), so registering
a DELIBERATELY passable bump in the one band that switch cannot suppress
would make it the single LEAST passable, least disarmable guard in the whole
suite -- exactly backwards for what this chunk is.

AC5 -- THE CROSS-REPO-MEMO CARVE-OUT IS UNCONDITIONAL, checked before
applicability, before the marker, before anything else: `cross-repo-memo`
is the plan's own sanctioned channel for repos a session does not own, so a
session already using the channel this guard exists to steer everyone
toward must never be nudged away from it. Matched by INVOKED EXECUTABLE
identity (`_command_invokes_cross_repo_memo`), never by a destination path
shape -- see that function's own docstring for why a `*/cross-repo/`-style
path match would be a hole, and this module's own test suite for the
negative case (a hand-rolled write into a sibling's `cross-repo/` directory
via `cp`/`tee`/etc. is NOT this executable and still bumps).

READS NEVER BUMP. Only a git WRITE subcommand (i.e. not a member of
`_GIT_READONLY_SUBCOMMANDS`) or a recognised plain-bash write-sink shape is
ever a candidate at all -- see `_iter_write_sink_candidates`.

Negative-spec:
  - Does NOT add fail-closed behaviour anywhere -- see § Design posture.
  - Does NOT add unforgeability machinery to the marker -- consumes C3's
    marker exactly as written, no creation guard, no identity gating.
  - Does NOT enumerate evasions -- no `-c`-payload recursion (that is C5's
    AC4, not this guard's), no brace-expansion handling, no adversarial
    interpreter-indirection unwrapping.
  - Does NOT compose a gitdir path -- every gitdir this module touches comes
    from `_write_bump_marker.resolve_gitdir` (`git rev-parse --git-dir`,
    resolved not composed).
  - Does NOT resolve applicability from the live payload `cwd` -- see
    "WHAT COUNTS AS..." above.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    ResolutionConfidence,
    normalize_executable_basename,
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._helpers import resolve_git_root
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._write_bump_applicability import (
    anchor_subtree_contains,
    bump_applies,
    is_agent_memory_store_path,
    publish_destination_owner,
    record_applicability_event,
    resolve_launch_anchor,
    target_is_publish_destination,
    target_is_registered_repo,
    target_is_under_claude_home,
)
from coordinator_core.bash_guards._write_bump_marker import (
    bump_is_cleared,
    effective_session_id,
    marker_gitdir_is_writable,
    resolve_gitdir,
)
from coordinator_core.bash_guards._write_bump_message import (
    AGENT_CLASS_SUBAGENT,
    DESTINATION_FOREIGN,
    DESTINATION_PUBLISH,
    render_bump_message,
    resolve_agent_class,
)
from coordinator_core.bash_guards._write_bump_sink_shapes import (
    PS_SET_LOCATION_ALIASES,
    _host_is_windows,
    extract_set_location_target_powershell,
    extract_write_sink_targets_for_segment,
    extract_write_sink_targets_powershell,
    nearest_existing_ancestor as _nearest_existing_ancestor,
    resolve_relative as _resolve_relative,
)
from coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist import (
    _GIT_READONLY_SUBCOMMANDS,
)
from coordinator_core.bash_guards.commit_tripwires import _same_tree
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.trusted_root_guard import _settings_home_dir_from_env
from coordinator_core.write_guards._case_fold_path import casefold_path

#: `git -C` flag shapes -- separate-token (`-C <dir>`) and attached
#: (`-C<dir>`), the same two forms every other `-C`/flag-value reader in
#: this package already recognises.
_DASH_C_ATTACHED_RE = re.compile(r"^-C(.+)$")

#: `--git-dir`/`--work-tree` attached (`--git-dir=<dir>`) forms -- the CLI
#: twins of the `GIT_DIR`/`GIT_WORK_TREE` env vars below. Real `git`
#: recognises both the `=`-attached and separate-token spellings for each;
#: both are handled in `_git_subcommand_and_target_cwd`.
_DASH_GIT_DIR_ATTACHED_RE = re.compile(r"^--git-dir=(.+)$")
_DASH_WORK_TREE_ATTACHED_RE = re.compile(r"^--work-tree=(.+)$")

#: `git -c <name>=<value>` -- attached (`-c<name>=<value>`, no space) and
#: separate-token (`-c <name>=<value>`, the common spelling) forms, the
#: SAME two shapes `-C` already gets. Review finding (2026-08-06): before
#: this fix, `-c` fell into the generic `tok.startswith("-")` skip branch
#: below, which consumes ONLY the `-c` token itself and never the following
#: `name=value` payload -- so that payload was misread as the git
#: SUBCOMMAND (an over-block/misclassification bug independent of
#: `core.worktree`) while `git -c core.worktree=<foreign> <verb> ...`'s real
#: target-relocating value was silently dropped entirely (the security
#: evasion: `target_cwd` stayed at the session's own anchor, so the
#: same-repo check passed and the guard allowed a write real `git` performs
#: against the foreign work tree named by `core.worktree`).
_DASH_C_LOWER_ATTACHED_RE = re.compile(r"^-c(.+)$")

#: `-c core.worktree=<dir>` is the ONLY `-c`-settable config key this
#: module found that relocates where a write lands (checked: `core.gitdir`
#: is not a real git config key -- there is no config-file equivalent of
#: `--git-dir`/`GIT_DIR`, only the CLI flag and env var, both already
#: handled above; `core.bare` marks a repo bare, it does not redirect a
#: target directory; `safe.directory` gates whether git will operate in a
#: directory at all, it does not name a write target). Real git's own docs
#: (`git-config(1)`, `core.worktree`): "The `GIT_WORK_TREE` environment
#: variable and the `--work-tree` command-line option can override this
#: configuration variable" -- i.e. `--work-tree` > `GIT_WORK_TREE` >
#: `core.worktree` (config, including `-c`-supplied), the precedence this
#: module's `cli_config_worktree` tier below is ordered to match.
_CORE_WORKTREE_CONFIG_RE = re.compile(r"^core\.worktree=(.*)$", re.IGNORECASE)

#: Other git global options that take a MANDATORY value and support both
#: the attached (`--flag=value`) and separate-token (`--flag value`)
#: spellings, per `git(1)`'s own OPTIONS section -- none of these redirect
#: a write target (`--namespace`/`--super-prefix` affect ref namespacing
#: and submodule recursion display only; `--config-env=<name>=<envvar>`
#: supplies a config value indirectly via an environment variable this
#: module does not attempt to resolve, since doing so correctly would
#: require re-deriving arbitrary env-var indirection -- out of scope, same
#: as the pre-existing "override values are never shell-expanded"
#: limitation this module already carries for `-C $VAR`/`GIT_DIR="$VAR"`).
#: Still MUST be skipped as a two-token unit here, or their mandatory value
#: leaks into the subcommand slot exactly like the `-c` bug above.
#: `--exec-path` is DELIBERATELY excluded -- `git(1)` documents it as
#: `--exec-path[=<path>]`, an OPTIONAL argument, which git's own
#: parse-options convention only ever accepts in the attached `=`-joined
#: form (never a separate token) -- so it already round-trips correctly
#: through the generic `tok.startswith("-")` skip branch below and adding
#: it here would wrongly swallow the NEXT real token whenever `--exec-path`
#: is used bare (no value).
_CONFIG_ENV_ATTACHED_RE = re.compile(r"^--config-env=(.+)$")
_NAMESPACE_ATTACHED_RE = re.compile(r"^--namespace=(.+)$")
_SUPER_PREFIX_ATTACHED_RE = re.compile(r"^--super-prefix=(.+)$")

#: A bare `NAME=value` env-assignment token, the shape `resolve_command_
#: positions` peels off the front of a segment into `raw_tokens` (see
#: `_command_tokenizer._peel_command_position`) without itself recording
#: WHICH names were assigned. This module re-derives that from `raw_tokens`
#: itself for exactly the three names below -- see `_env_repo_overrides`.
_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

#: THE EVASION THIS MODULE CLOSES (found 2026-08-06, live dispatch
#: incident): `GIT_DIR=<foreign>/.git git cat-file -t HEAD` -- and, by the
#: identical hole, `GIT_DIR=<foreign>/.git git commit ...` -- reached a
#: foreign repo while every candidate-extraction path above (`-C`, `cd &&`)
#: kept computing the target purely from the session's `cwd`. `git` itself
#: honours `GIT_DIR`/`GIT_WORK_TREE`/`GIT_COMMON_DIR` (env) and
#: `--git-dir=`/`--work-tree=` (CLI, which additionally override the env
#: forms) REGARDLESS of `cwd` -- a command that never `cd`s and never
#: passes `-C` can still operate on an entirely different repo through
#: either spelling. `_command_tokenizer.resolve_command_positions` already
#: peels a leading `GIT_DIR=...` (or an `env GIT_DIR=... git ...` wrapper)
#: off the front of `rc.tokens` when resolving the command HEAD (correctly
#: recognising the invoked binary is `git`) -- but it does not, and by its
#: own docstring's design should not, interpret what that assignment MEANS
#: for a target-repo resolution; that interpretation is this guard's job,
#: and until this fix it was never done. `rc.raw_tokens` still carries the
#: peeled assignment verbatim (see `ResolvedCommand.raw_tokens`'s own
#: docstring, "WITHOUT also inheriting..." -- the exact seam this reuses),
#: so no second tokenization pass is needed to recover it.
_GIT_DIR_ENV_NAMES = ("GIT_DIR", "GIT_COMMON_DIR")
_WORK_TREE_ENV_NAME = "GIT_WORK_TREE"


def _deny(reason: str) -> Dict[str, Any]:
    """Same envelope shape as `dispatch_checks._deny` -- inlined rather than
    imported so this module has no dependency on that ~5500-line file for
    two lines of dict literal."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _resolve_and_casefold(raw: str) -> Optional[str]:
    """`os.path.realpath` then `casefold_path`, or `None` on any resolution
    failure. Mirrors `_write_bump_applicability._resolve_path` exactly (the
    sibling module in this same wave) so the two never disagree about what
    "resolved" means for a path comparison."""
    if not raw:
        return None
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        return None
    return casefold_path(resolved)


#: `_nearest_existing_ancestor` -- LIFTED to `_write_bump_sink_shapes.
#: nearest_existing_ancestor` during C5's own landing (imported above as
#: `_nearest_existing_ancestor` for call-site/test-attribute compatibility)
#: so C5 (`bump_outside_repo_write.py`) shares the identical helper rather
#: than carrying a second copy. See that function's own docstring for the
#: latent-bug history this fix addresses.


def _same_repo_root(path_a: str, path_b: str) -> bool:
    """Two resolved paths name "the same repo" for this guard's purposes.

    Reuses `commit_tripwires._same_tree`'s own `os.path.realpath`-equality
    comparison (per this chunk's own reuse instruction) -- called on the
    CASE-FOLDED form of both operands, since `_same_tree`'s own `os.path.
    normcase` is a no-op on POSIX/macOS and this fleet's primary filesystem
    (APFS) is case-insensitive-but-case-preserving (Anti-scope: "Both
    operands resolved and case-folded"). `_resolve_and_casefold` already
    performs the realpath+casefold this module's own comparisons need, so
    this function operates on ALREADY-RESOLVED strings -- callers pass
    already-casefolded values in from `_resolve_and_casefold`, and this
    function's own `_same_tree` call resolves them AGAIN (harmless:
    `os.path.realpath` on an already-resolved absolute path is a no-op)
    purely to keep this one call site as the single "are these the same
    repo" predicate, rather than a bare `==` scattered at each call site.

    AC14 -- callers MUST pass the COMMON-dir form of each side (see
    `_common_dir_cf`), never the per-worktree PRIVATE gitdir `resolve_gitdir`
    returns: `--git-dir` differs per linked worktree of the SAME repo
    (`<main>/.git/worktrees/<name>`), which would classify a linked worktree
    of the session's own anchor repo as a foreign one -- a live false
    positive at HEAD, fixed by this chunk. `sessions_dir` (the session hub's
    own repo-identity comparison) already resolves `--git-common-dir` for
    exactly this reason; this predicate now agrees with it.
    """
    return _same_tree(path_a, path_b)


#: Adjacent, case-folded directory-path pair this predicate keys on -- mirrors
#: `bump_out_of_repo_tool_write._LESSONS_OUTBOX_SEGMENTS` exactly (same two
#: literal segments, same reasoning) so the two surfaces cannot silently
#: drift on what "lessons-outbox" means path-shape-wise. Deliberately the two
#: literal segments only, never a broader `cross-repo/` prefix -- see
#: `_target_is_lessons_outbox_write`'s own docstring, "DO NOT WIDEN".
_LESSONS_OUTBOX_SEGMENTS = ("state", "lessons-outbox")


def _target_is_lessons_outbox_write(file_path: str) -> bool:
    """True iff `file_path`'s path components contain `state/lessons-outbox`
    as an adjacent, case-folded directory pair -- the Bash-surface twin of
    `bump_out_of_repo_tool_write._target_is_lessons_outbox_write` (same
    module docstring section there, "LESSONS-OUTBOX IS NOT A MISWRITE, EVEN
    THOUGH IT IS A FOREIGN REPO"). `coordinator-lesson-promote`
    (`ops/queue_promote.py`) writes a universal lesson's durable home to
    `<doe_root>/state/lessons-outbox/<id>.yaml` BY DESIGN -- example-doctrine-repo is
    the central lessons repo, there is no in-repo alternative, and a foreign-
    repo bump on that write is a false positive on both surfaces alike.
    Callers treat `True` as "never bump".

    DELIBERATELY NOT CONJUNCTIVE WITH "NO GIT REPO", unlike this module's
    sibling `_target_is_bare_temp_scratch`-shaped exemptions (see the tool
    surface's own docstring, same section, for why): the whole point of this
    exemption is that the target IS a foreign repo -- `queue.promote` always
    writes into an actual example-doctrine-repo checkout -- so gating on "no repo"
    would exempt nothing real. This is exactly why the check below sits
    INSIDE the per-candidate loop, after `target_gitdir` is already
    confirmed non-`None`: this guard (C4) only ever sees candidates that
    resolve to a real git root in the first place (an unresolvable target is
    C5's `bump_outside_repo_write.py` concern, never this module's), so
    there is no "no repo" branch to gate against here even in principle.

    DO NOT WIDEN THIS TO `cross-repo/inbox/` OR `cross-repo/outbox/` -- see
    the tool-surface twin's own docstring, "DO NOT WIDEN THIS", verbatim:
    hand-writing a memo into a sibling's tree stays forbidden, and the
    `cross-repo-memo` carve-out (AC5, above) already handles the sanctioned
    memo CLI on THIS surface by invoked-executable identity, not destination
    path shape -- a second, path-shape-keyed exemption for the memo channel
    would blur a distinction this repo's own CLAUDE.md draws on purpose.

    Path-shape only, via a simple casefolded split -- matches a subdirectory
    under `state/lessons-outbox/` too, not merely a direct child file. Never
    raises: `casefold_path` and plain `str.split` do not raise on any `str`
    input.
    """
    if not file_path:
        return False
    normalized = casefold_path(file_path)
    parts = [p for p in normalized.split("/") if p]
    for i in range(len(parts) - 1):
        if (parts[i], parts[i + 1]) == _LESSONS_OUTBOX_SEGMENTS:
            return True
    return False


def _common_dir_cf_from_root(root: Optional[str]) -> Optional[str]:
    """The resolved+case-folded git COMMON dir for a git repo TOPLEVEL
    already resolved by the caller (`root`), or `None` when `root` is
    falsy or the common dir cannot be resolved.

    AC14 fix -- given the repo TOPLEVEL (`git rev-parse --show-toplevel`),
    resolves the common dir via `git.git_dir.resolve_git_common_dir` (the
    pure-Python, no-subprocess common-dir resolver `sessions_dir` reasons
    about), rather than reusing `resolve_gitdir`'s per-worktree PRIVATE
    gitdir (`--git-dir`) for this comparison -- see `_same_repo_root` for
    why that distinction is exactly what AC14 is about. A linked worktree's
    toplevel resolves to its own private gitdir-pointer FILE (not `.git`
    directory); `resolve_git_common_dir` follows that pointer's `commondir`
    file to the shared common dir, giving every worktree of one repo the
    SAME value here -- the property `_same_repo_root` needs.

    Split out from a single `_common_dir_cf(path)` that re-resolved
    `resolve_git_root` (a `git rev-parse --show-toplevel` subprocess) on
    every call -- callers now resolve the toplevel ONCE and pass it in
    here, so a candidate whose toplevel is also needed elsewhere (e.g. the
    message's `target_repo_label`) pays for that subprocess once, not
    twice, on the per-write-sink-candidate hot path.
    """
    if not root:
        return None
    common = resolve_git_common_dir(root)
    return _resolve_and_casefold(str(common))


# ---------------------------------------------------------------------------
# AC5 -- the cross-repo-memo carve-out, unconditional and checked first.
# ---------------------------------------------------------------------------


def _cross_repo_memo_executable_path(env: Optional[dict] = None) -> str:
    """`${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/cross-repo-memo`,
    resolved via the package's own shared settings-home helper (never a
    hand-rolled env read -- see `_settings_home_dir_from_env`'s own
    docstring for the `COORDINATOR_SETTINGS_HOME` -> `CLAUDE_HOME`/`HOME`
    precedence this reuses). Returns `""` (fail open) when the settings
    home itself cannot be resolved."""
    env = os.environ if env is None else env
    home = _settings_home_dir_from_env(env)
    if not home:
        return ""
    return os.path.join(home, "bin", "cross-repo-memo")


def _command_invokes_cross_repo_memo(
    cmd: str, cwd: Optional[str], env: Optional[dict] = None
) -> bool:
    """True iff any top-level segment of `cmd` invokes the `cross-repo-memo`
    CLI -- the unconditional AC5 carve-out.

    Matched by INVOKED EXECUTABLE IDENTITY, never a destination path shape
    (see module docstring, "AC5"): a BARE-word invocation (`cross-repo-memo
    ...`, the ordinary PATH-resolved form) matches on basename identity
    alone via `token_matches_binary`, the same boundary-anchored matcher
    every other binary-identity check in this package already uses. A
    PATH-CARRYING invocation (contains a separator) additionally must
    resolve to the canonical settings-home location -- a same-named script
    living somewhere else on disk does NOT get this carve-out, closing the
    exact hole a bare-basename-only match would open for a same-named
    decoy. Only depth-0 (top-level) segments are inspected -- `-c`-payload
    recursion is explicitly out of scope here (C5's AC4, not this guard's).
    """
    if not cmd or not cmd.strip():
        return False
    try:
        resolved_segments = resolve_command_positions(cmd)
    except Exception:  # noqa: BLE001 -- fail open, never let a parse crash reach the dispatcher
        return False
    canonical = _cross_repo_memo_executable_path(env)
    for rc in resolved_segments:
        if rc.depth != 0 or not rc.tokens:
            continue
        head = rc.tokens[0]
        if not token_matches_binary(head, "cross-repo-memo"):
            continue
        if "/" not in head and "\\" not in head:
            # Bare word -- trust PATH resolution the same way every other
            # basename-only matcher in this package already does.
            return True
        candidate = head
        if not os.path.isabs(candidate):
            candidate = os.path.join(cwd or os.getcwd(), candidate)
        candidate_cf = _resolve_and_casefold(candidate)
        canonical_cf = _resolve_and_casefold(canonical) if canonical else None
        if candidate_cf is not None and canonical_cf is not None and candidate_cf == canonical_cf:
            return True
    return False


# ---------------------------------------------------------------------------
# Candidate extraction -- git -C / cd&&git / plain-bash write sinks.
# ---------------------------------------------------------------------------


def _env_repo_overrides(raw_tokens: List[str], resolved_tokens: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """`(git_dir_override, work_tree_override)` recovered from the
    env-assignment prefix `_command_tokenizer._peel_command_position`
    stripped off the front of this segment -- i.e. `raw_tokens`'s leading
    slice that is no longer present in `resolved_tokens` (peeling only ever
    removes tokens from the FRONT; see `_peel_command_position`'s own
    "repeating until nothing more strips" loop, which never reorders or
    modifies a token it keeps). Both env forms this scans for
    (`GIT_DIR=`/`GIT_COMMON_DIR=`/`GIT_WORK_TREE=`) survive an `env GIT_DIR=
    ... git ...` wrapper spelling too, since `env`'s own peeling branch in
    `_peel_command_position` also strips its `NAME=value` arguments from the
    same front slice.

    Last assignment of a given name wins (real shell/env semantics:
    `GIT_DIR=a GIT_DIR=b git ...` -- `git` sees `b`), scanned in raw token
    order. Never resolved to an absolute path here -- callers resolve the
    result the same way every other candidate target is resolved
    (`_resolve_relative` against whatever `cwd_for_git` the CLI-flag walk
    has reached, since `-C`/`--git-dir`/`--work-tree`/env can all combine on
    one real invocation and CLI flags take precedence over env, exactly as
    real `git` resolves them).

    ACCEPTED, NAMED LIMITATION (review finding, P3, confirmed not an
    oversight): a value is used LITERALLY, never shell-expanded --
    `GIT_DIR="$SOME_VAR"`/`GIT_DIR=$(cmd)`-shaped payloads pass through
    `_ENV_ASSIGNMENT_RE` as their raw unexpanded text, which will not name a
    real filesystem path, so the resulting candidate falls through this
    guard's ordinary "no existing ancestor"/"not inside any git repo"
    fail-open paths (`_nearest_existing_ancestor`/`resolve_gitdir`
    returning `None`) rather than a distinct unresolvable-target deny.
    Pre-existing (identical gap for `-C $VAR`, not introduced by this
    module's env-override addition) and consistent with this module's own
    "FAIL OPEN, EVERYWHERE" design posture (module docstring) -- this is a
    passable speed bump, not a security boundary, and a session using shell
    indirection to name its OWN target is not the evasion shape this module
    exists to catch. Not fixed here.
    """
    prefix_len = len(raw_tokens) - len(resolved_tokens)
    if prefix_len <= 0:
        return None, None
    git_dir: Optional[str] = None
    work_tree: Optional[str] = None
    for tok in raw_tokens[:prefix_len]:
        m = _ENV_ASSIGNMENT_RE.match(tok)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if name in _GIT_DIR_ENV_NAMES:
            git_dir = value
        elif name == _WORK_TREE_ENV_NAME:
            work_tree = value
    return git_dir, work_tree


def _worktree_root_for_gitdir_override(resolved_override: str) -> str:
    """A `GIT_DIR`/`--git-dir` override names the repo's git-metadata
    directory itself (conventionally `<repo>/.git`), NOT a directory this
    guard's downstream resolvers (`resolve_gitdir`/`resolve_git_root`, both
    plain `git -C <dir> rev-parse ...` subprocess calls) can run FROM: `git
    rev-parse --show-toplevel` fails outright with cwd set to a `.git`
    directory itself ("fatal: this operation must be run in a work tree",
    confirmed empirically) -- and `resolve_git_root`'s failure there would
    make `_common_dir_cf_from_root` return `None`, defeating the AC14
    same-repo-toplevel comparison for the SESSION'S OWN repo when it uses
    this override convention (a real over-block: an ordinary in-repo `git
    --git-dir=$(pwd)/.git ...` command would then misclassify its own repo
    as foreign).

    Mirrors the `.git`-suffix convention this package already special-cases
    elsewhere (`bump_outside_repo_write.check_bump_outside_repo_write`'s own
    `anchor_git_root_str = str(anchor_gitdir.parent) if anchor_gitdir.name
    == ".git" else str(anchor_gitdir)`): when the override's basename is
    literally `.git`, resolve against its PARENT (the conventional worktree
    root) instead, so downstream resolution runs from a real worktree
    exactly as it would for an equivalent `-C <worktree>` invocation. Any
    other override shape (a bare-repo gitdir with no `.git` suffix, or a
    `GIT_WORK_TREE` override, which already names a worktree directly) is
    left verbatim -- this narrow special-case only ever fixes the ordinary
    convention's own-repo false positive, never widens what counts as
    "foreign".
    """
    if os.path.basename(resolved_override.rstrip("/\\")) == ".git":
        parent = os.path.dirname(resolved_override.rstrip("/\\"))
        return parent or resolved_override
    return resolved_override


def _git_subcommand_and_target_cwd(
    tokens: List[str],
    effective_cwd: str,
    raw_tokens: Optional[List[str]] = None,
) -> Tuple[Optional[str], str]:
    """`tokens[0]` is already known to be `git` (basename-normalized).
    Returns `(subcommand, target_cwd)` -- the first non-flag token after
    consuming any `-C <dir>`/`-C<dir>` global option(s) (each resolved in
    turn, relative to the PREVIOUS resolved dir, matching real `git -C`
    chaining semantics), and every other leading `-`-prefixed global option
    skipped WITHOUT attempting to know whether it consumes a separate-token
    value of its own (Anti-scope: "do not enumerate evasions" -- the
    fail-open consequence of under-skipping an option's value is that this
    function returns a wrong `subcommand`, which just means the guard
    either misses a real write [never a false bump] or treats an option
    VALUE as the subcommand [which will not match any entry in `_GIT_
    READONLY_SUBCOMMANDS` and so, in the fail-toward-bump-on-unknown
    direction this module takes for git subcommands specifically, could
    read as a write -- an acceptable, narrow imprecision for a passable
    bump, not a security property]).

    `GIT_DIR`/`--git-dir`/`GIT_COMMON_DIR`/`GIT_WORK_TREE`/`--work-tree`
    EVASION FIX -- `-C` is no longer the only way this function's `cwd_for_
    git` can move off `effective_cwd`. A `--git-dir=<dir>`/`--git-dir <dir>`
    CLI flag anywhere in `tokens` (git accepts it at any global-option
    position, not only before `-C`) is tracked the same way `-C` already is;
    `--work-tree=<dir>`/`--work-tree <dir>` likewise. `raw_tokens` (when
    given -- `None` from any caller that has none, e.g. a direct unit-test
    invocation) additionally supplies `GIT_DIR=`/`GIT_COMMON_DIR=`/
    `GIT_WORK_TREE=` env-assignment overrides via `_env_repo_overrides`.
    Precedence, matching real `git` (CLI overrides env; `--git-dir`/`GIT_
    DIR` outrank `--work-tree`/`GIT_WORK_TREE`, since a git-dir override
    alone is sufficient to name a whole different repo while a work-tree
    override alone is not; `-c core.worktree=` is lowest of all, per
    `git-config(1)`'s own "`GIT_WORK_TREE`/`--work-tree` can override this
    configuration variable"): CLI `--git-dir` > env `GIT_DIR`/`GIT_COMMON_DIR`
    > CLI `--work-tree` > env `GIT_WORK_TREE` > `-c core.worktree=` > the
    `-C`-walked `cwd_for_git` computed above. Whichever one resolves is
    applied ONCE, at the end, relative to the `-C`-walked `cwd_for_git` --
    not per-occurrence inline -- since (unlike `-C`, which real `git` chains
    left-to-right) `--git-dir`/`--work-tree`/the env vars/`-c core.worktree=`
    are each a single absolute-or-cwd-relative target, not a chain.

    `-c`/`--config-env`/`--namespace`/`--super-prefix` EACH CONSUME THEIR
    OWN MANDATORY VALUE TOKEN -- review finding (2026-08-06): before this
    fix, ALL of these fell into the generic `tok.startswith("-")` skip
    branch, which advances `i` by exactly one regardless of whether the
    flag takes a value, so a separate-token spelling's value token was
    misread as the SUBCOMMAND itself on the very next loop iteration. For
    `-c core.worktree=<foreign>` specifically this was also a security
    evasion (the identical class this whole diff exists to close): the
    relocating value was dropped entirely rather than merely misparsed, so
    `target_cwd` stayed at the session's own anchor and the guard silently
    allowed a write real `git` performs against the foreign work tree.
    `--exec-path` is the one documented value-taking-looking flag
    deliberately NOT added to this list -- see `_CONFIG_ENV_ATTACHED_RE`'s
    own module-level comment for why.
    """
    cwd_for_git = effective_cwd
    cli_git_dir: Optional[str] = None
    cli_work_tree: Optional[str] = None
    cli_config_worktree: Optional[str] = None
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-C":
            if i + 1 < n:
                # `None` means untranslatable -- leave `cwd_for_git` at its
                # previous value rather than clobbering it with `None`.
                resolved = _resolve_relative(cwd_for_git, tokens[i + 1])
                if resolved is not None:
                    cwd_for_git = resolved
                i += 2
                continue
            i += 1
            continue
        m = _DASH_C_ATTACHED_RE.match(tok)
        if m:
            resolved = _resolve_relative(cwd_for_git, m.group(1))
            if resolved is not None:
                cwd_for_git = resolved
            i += 1
            continue
        if tok == "--git-dir":
            if i + 1 < n:
                cli_git_dir = tokens[i + 1]
                i += 2
                continue
            i += 1
            continue
        m = _DASH_GIT_DIR_ATTACHED_RE.match(tok)
        if m:
            cli_git_dir = m.group(1)
            i += 1
            continue
        if tok == "--work-tree":
            if i + 1 < n:
                cli_work_tree = tokens[i + 1]
                i += 2
                continue
            i += 1
            continue
        m = _DASH_WORK_TREE_ATTACHED_RE.match(tok)
        if m:
            cli_work_tree = m.group(1)
            i += 1
            continue
        m = _DASH_C_LOWER_ATTACHED_RE.match(tok)
        if m:
            cm = _CORE_WORKTREE_CONFIG_RE.match(m.group(1))
            if cm:
                cli_config_worktree = cm.group(1)
            i += 1
            continue
        if tok == "-c":
            if i + 1 < n:
                cm = _CORE_WORKTREE_CONFIG_RE.match(tokens[i + 1])
                if cm:
                    cli_config_worktree = cm.group(1)
                i += 2
                continue
            i += 1
            continue
        if (
            _CONFIG_ENV_ATTACHED_RE.match(tok)
            or _NAMESPACE_ATTACHED_RE.match(tok)
            or _SUPER_PREFIX_ATTACHED_RE.match(tok)
        ):
            i += 1
            continue
        if tok in ("--config-env", "--namespace", "--super-prefix"):
            i += 2 if i + 1 < n else 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        env_git_dir, env_work_tree = (
            _env_repo_overrides(raw_tokens, tokens) if raw_tokens is not None else (None, None)
        )
        git_dir_override = cli_git_dir or env_git_dir
        override = git_dir_override or cli_work_tree or env_work_tree or cli_config_worktree
        if override:
            resolved = _resolve_relative(cwd_for_git, override)
            # `None` means untranslatable -- leave `cwd_for_git` at its
            # previous value rather than clobbering it with `None`.
            if resolved is not None:
                cwd_for_git = (
                    _worktree_root_for_gitdir_override(resolved) if git_dir_override else resolved
                )
        return tok, cwd_for_git
    env_git_dir, env_work_tree = (
        _env_repo_overrides(raw_tokens, tokens) if raw_tokens is not None else (None, None)
    )
    git_dir_override = cli_git_dir or env_git_dir
    override = git_dir_override or cli_work_tree or env_work_tree or cli_config_worktree
    if override:
        resolved = _resolve_relative(cwd_for_git, override)
        # `None` means untranslatable -- leave `cwd_for_git` at its
        # previous value rather than clobbering it with `None`.
        if resolved is not None:
            cwd_for_git = (
                _worktree_root_for_gitdir_override(resolved) if git_dir_override else resolved
            )
    return None, cwd_for_git


def _iter_write_sink_candidates(
    cmd: str, cwd: Optional[str]
) -> Iterator[Tuple[str, str, Optional[str]]]:
    """Yield `(target_dir, label, raw_target)` for every depth-0 segment of
    `cmd` that is either a git WRITE subcommand (`git -C <dir> <sub>` / `cd
    <dir> && git <sub>`, tracked via a running `effective_cwd` that a `cd`
    segment updates for later segments in the SAME compound command) or a
    recognised plain-bash write-sink shape (`_write_bump_sink_shapes`).

    `target_dir` is NOT YET resolved to a git root -- callers do that via
    `resolve_gitdir`. Reads (a git subcommand in `_GIT_READONLY_SUBCOMMANDS`)
    are never yielded at all (module docstring, "READS NEVER BUMP").

    `resolve_command_positions` is called with `preserve_windows_
    backslashes=_host_is_windows()` (C2, docs/plans/2026-08-10-carve-claude-
    out-and-close-the-backslash-bypass.md, AC5-AC6) -- on a Windows host,
    an UNQUOTED `\\`-spelled absolute target (`C:\\Users\\...\\out.txt`)  # abs-path-ok: illustrative example shape, not a machine-specific citation
    survives tokenization with its separators intact, instead of `shlex`'s
    ordinary POSIX-escape rule silently eating every backslash and leaving
    a single mangled token (`C:Users...out.txt`) that neither
    `_WINDOWS_DRIVE_ABSOLUTE_RE` nor `translate_msys_path` can recognise as
    absolute -- the mangled token was falling through to a cwd-relative
    join, misclassifying a foreign-repo write as a same-repo one. Fixed at
    THIS single upstream seam (see `tokenize_full_command`'s own docstring)
    rather than at `_resolve_relative`/`translate_msys_path` below, because
    the lost separators cannot be recovered once `shlex` has already
    consumed them -- there is no later point in this pipeline where the
    original spelling still exists to normalize.

    `raw_target` (R1, docs/plans/2026-08-08-the-bump-message-never-showed-
    the-operat.md) is the literal token this segment carried BEFORE
    `_resolve_relative`/`translate_msys_path` touched it, for the plain-
    bash write-sink shape -- captured at extraction, never reconstructed
    (AC2). For a `git` WRITE subcommand, `target_dir` is a CWD `-C`/
    `--git-dir`/`--work-tree`/env-override CHAIN, not a single typed token
    naming the write target -- there is no one raw string to show without
    inventing one, so this leg yields `None` for `raw_target` rather than
    synthesising a candidate (R1's own instruction: "if the raw token is
    genuinely unavailable ... say so ... rather than synthesising one").

    Only depth-0 segments are inspected -- a segment recovered from inside
    a `$( )`/backtick substitution or an interpreter `-c` payload (depth>0)
    is skipped, matching this chunk's own scope (`-c`-payload write-sink
    classification is C5's AC4, not C4's).
    """
    if not cmd or not cmd.strip():
        return
    try:
        resolved_segments = resolve_command_positions(
            cmd, preserve_windows_backslashes=_host_is_windows()
        )
    except Exception:  # noqa: BLE001 -- fail open, never let a parse crash reach the dispatcher
        return

    effective_cwd = cwd or os.getcwd()
    for rc in resolved_segments:
        if rc.depth != 0:
            continue
        if rc.confidence == ResolutionConfidence.UNRESOLVED or not rc.tokens:
            continue
        head_base = normalize_executable_basename(rc.tokens[0])

        if head_base == "cd":
            positional = [t for t in rc.tokens[1:] if not t.startswith("-")]
            if len(positional) == 1:
                # `None` means untranslatable -- a `cd` we cannot resolve
                # must not poison every subsequent candidate in this
                # segment, so leave `effective_cwd` at its previous value.
                resolved = _resolve_relative(effective_cwd, positional[0])
                if resolved is not None:
                    effective_cwd = resolved
            continue

        if head_base == "git":
            subcommand, target_cwd = _git_subcommand_and_target_cwd(
                rc.tokens, effective_cwd, raw_tokens=rc.raw_tokens
            )
            if subcommand and subcommand not in _GIT_READONLY_SUBCOMMANDS:
                yield (target_cwd, "git %s" % subcommand, None)
            continue

        for raw_target in extract_write_sink_targets_for_segment(rc.tokens, head_base):
            resolved_target = _resolve_relative(effective_cwd, raw_target)
            if resolved_target is None:
                continue
            yield (resolved_target, head_base, raw_target)


# ---------------------------------------------------------------------------
# Sandbox-root message hint (subagent-class copy only).
# ---------------------------------------------------------------------------


def _sandbox_root_hint(git_root: Optional[str], session_id: str) -> str:
    """`state/subagent-share/<session_id>/` under `git_root` -- this
    fleet's own current subagent-share convention (`coordinator_core.
    subagent_sandbox.provision_report`, e.g. the exact path shape at that
    module's line ~622), reused here as the message's "sandbox" pointer
    rather than a literal. Not a claim of enforcement -- see `write_guards.
    engine.py:32-37` (cited in this wave's sibling modules): the prior
    hard-deny write-outside-sandbox confinement was removed 2026-07-15, so
    this is an OFFERED route, not a guaranteed one; the message text itself
    (C6, `_write_bump_message.render_subagent_message`) already says so.
    Returns `""` (the message module's own default) when `git_root` or
    `session_id` is empty -- no fabricated path for an unresolvable case.
    """
    if not git_root or not session_id:
        return ""
    return str(Path(git_root) / "state" / "subagent-share" / session_id)


# ---------------------------------------------------------------------------
# The shared per-candidate JUDGMENT (AC9 -- the thing this leg must NOT
# re-derive, only feed). Factored out of `check_bump_foreign_repo_write`'s
# own loop body so the Bash and PowerShell legs share one verdict predicate
# and can only ever differ in how a candidate `target_dir` is EXTRACTED --
# see this guard's own dialect-gate comment below and the sibling
# `bump_outside_repo_write.py` for why that split matters: this guard's own
# defining predicate (a candidate resolving to SOME OTHER git root) lives
# entirely in this function, unchanged by which dialect found the
# candidate.
# ---------------------------------------------------------------------------


def _evaluate_foreign_repo_candidate(
    target_dir: str,
    session_id: str,
    payload: Dict[str, Any],
    anchor: str,
    anchor_has_repo: bool,
    anchor_common_cf: Optional[str],
    env: dict,
    agent_id: str,
    raw_target: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """One candidate write-sink `target_dir`, already resolved to an
    absolute-or-cwd-relative string by the caller's own extraction leg,
    judged against this guard's defining predicate -- returns a deny
    envelope, or `None` when the candidate does not bump, exactly as
    `check_bump_foreign_repo_write`'s own loop body did before this
    factor-out. See that function's
    docstring for the ordered-checks summary this preserves verbatim.

    `raw_target` (R1) is the caller's own PRE-translation token for this
    candidate, or `None` when the caller's extraction leg has no single raw
    token to offer (see `_iter_write_sink_candidates`'s own docstring for
    the git-subcommand case). Threaded into `render_bump_message` only when
    it differs from `target_dir` -- identical values collapse to the single
    display form (AC2's "never print the same string twice" instruction)."""
    probe_dir = _nearest_existing_ancestor(target_dir)
    if probe_dir is None:
        # No existing ancestor at all -- cannot resolve a git root
        # either way; fail open (see `_nearest_existing_ancestor`).
        return None
    target_gitdir = resolve_gitdir(probe_dir)
    if target_gitdir is None:
        # Not inside any git repo at all -- an outside-repo write, C5's
        # concern, never this guard's.
        return None

    # Lessons-outbox exemption -- a DIFFERENT axis from the AC5
    # cross-repo-memo carve-out (checked by the caller before this
    # function ever runs). This exemption matches on DESTINATION PATH
    # SHAPE, and can only be evaluated once a candidate's `target_gitdir`
    # is known to be non-`None`. It also sits per-CANDIDATE rather than
    # per-command: a compound command could write both an ordinary
    # foreign-repo target and a lessons-outbox one, and only the latter
    # should be silenced.
    if _target_is_lessons_outbox_write(target_dir):
        return None

    # Agent memory store -- Claude Code's own per-project persistent
    # memory (`<home>/.claude/projects/<slug>/memory/**`), never a
    # sibling repo or a cross-repo delivery even though `~/.claude` is
    # itself a git checkout on this fleet -- see
    # `is_agent_memory_store_path`'s own docstring for the false
    # positive this closes.
    if is_agent_memory_store_path(target_dir):
        return None

    # ~/.claude carve-out (docs/plans/2026-08-10-carve-claude-out-and-
    # close-the-backslash-bypass.md, C1, AC1-AC4). Reached here despite
    # `target_gitdir` already being confirmed non-`None` above -- `~/.claude`
    # IS a real git checkout on this fleet, matching the agent-memory
    # exemption immediately above; see `target_is_under_claude_home`'s own
    # docstring for why this is unconditional rather than gated on git-dir
    # state.
    if target_is_under_claude_home(target_dir):
        return None

    # Resolved once per candidate, reused below for the AC14 common-dir
    # comparison (when `anchor_has_repo`) and unconditionally for
    # `target_repo_label` -- see `_common_dir_cf_from_root`'s docstring
    # for why this used to cost a second subprocess spawn per candidate.
    probe_root = resolve_git_root(probe_dir)

    if anchor_has_repo:
        # AC14 -- compare COMMON dirs (worktree-safe), matching
        # `sessions_dir`'s own comparison; see `_common_dir_cf_from_root`.
        target_common_cf = _common_dir_cf_from_root(probe_root)
        if (
            anchor_common_cf is not None
            and target_common_cf is not None
            and _same_repo_root(anchor_common_cf, target_common_cf)
        ):
            return None
        # C3 -- the marker moves to the TARGET's own gitdir, not the
        # session anchor's: a target git repo is confirmed to exist at
        # this point (`target_gitdir is not None`, checked above), so
        # this branch now generalizes the no-repo-anchor branch's own
        # `marker_probe = probe_dir` fallback below, per-(session,
        # target) rather than per-session (AC4; see
        # `_write_bump_marker`'s "MARKER SCOPE" docstring section).
        marker_probe = probe_dir
    else:
        # § No-repo anchor branch -- the session itself owns no repo. A
        # REGISTERED target still bumps unconditionally, as it always has
        # (per § Where the bump does not fire). Narrow (PM ruling
        # 2026-08-10): an UNREGISTERED target now ALSO bumps unless it
        # sits at or under the session's own anchor SUBTREE -- see
        # `anchor_subtree_contains`'s own docstring for why this branch
        # (target already confirmed to resolve to a real gitdir, above) is
        # exactly the shape Narrow can site a clearable marker for. The
        # marker falls back to the TARGET's own gitdir either way (see
        # module docstring for why).
        if not target_is_registered_repo(
            str(target_gitdir), env=env
        ) and anchor_subtree_contains(anchor, target_dir):
            return None
        # `probe_dir`, not `target_dir` -- same latent-bug fix as
        # above: `resolve_git_root`/`bump_is_cleared` also shell out
        # with this as `cwd`, which requires an EXISTING directory.
        marker_probe = probe_dir

    marker_probe_root = resolve_git_root(marker_probe)
    if bump_is_cleared(
        marker_probe, session_id, git_root=marker_probe_root, agent_id=agent_id
    ):
        return None

    marker_gitdir = resolve_gitdir(marker_probe)
    if marker_gitdir is None or not marker_gitdir_is_writable(marker_gitdir):
        # Cannot compose a clear line without a gitdir, OR the target
        # gitdir exists but is not writable/readable (STAFF-ENG F0, AC5)
        # -- fail open on the WRITE axis in BOTH cases: allow, rather
        # than print a message the reader can never satisfy. See
        # `_write_bump_marker.marker_gitdir_is_writable`'s own docstring
        # -- a read-only target `.git` (a mirror synced under another
        # uid) takes the identical disposition as an unresolvable one.
        return None

    agent_class = resolve_agent_class(payload if isinstance(payload, dict) else {}, marker_probe_root)
    sandbox_root = (
        _sandbox_root_hint(marker_probe_root, effective_session_id(session_id, marker_probe_root, agent_id))
        if agent_class == AGENT_CLASS_SUBAGENT
        else ""
    )
    effective_sid = effective_session_id(session_id, marker_probe_root, agent_id)

    target_repo_label = probe_root or target_dir
    session_repo_label = resolve_git_root(anchor) or anchor

    # C1 -- classify the target as a registered PUBLISH destination or
    # an ordinary FOREIGN source repo (§ Design's three-class table;
    # OUTSIDE_ANY_REPO is never reached here, since this guard only ever
    # sees a resolved `target_gitdir` -- that predicate is C5's). Closed-
    # set membership decides the verdict; `owner` is context only for
    # C2's copy, never gating (see `target_is_publish_destination`'s own
    # docstring).
    destination_class = (
        DESTINATION_PUBLISH
        if target_is_publish_destination(target_repo_label, env=env)
        else DESTINATION_FOREIGN
    )
    destination_owner = (
        publish_destination_owner(target_repo_label, env=env)
        if destination_class == DESTINATION_PUBLISH
        else ""
    )

    # Finding #3 -- `cwd=anchor`, NOT the live payload `cwd`: the live
    # `cwd` is (in this guard's own canonical scenario) the FOREIGN
    # target repo, which would land this observability log in the very
    # repo the guard is bumping the write away from. This binds the LOG
    # ONLY, never the marker -- the two are different in kind. The
    # OPERATOR writes the marker via a deliberate `touch` (an
    # affirmative clear act the operator chooses to perform against a
    # specific target -- see C3, `marker_probe = probe_dir` above,
    # narrowed per-(session, target) rather than per-session); the
    # GUARD writes this observability log unbidden, on every fire, with
    # no operator choice involved. "The guard must not write into the
    # repo it is bumping you away from" binds THIS call site (still
    # `cwd=anchor`, still anchor-homed, still never the foreign one) --
    # it does not bind the marker's own siting, so narrowing where the
    # marker lives is not a contradiction of that principle, it is
    # orthogonal to it.
    record_applicability_event(
        session_id,
        repo=session_repo_label,
        target=target_repo_label,
        agent_class=agent_class,
        cwd=anchor,
    )

    message = render_bump_message(
        agent_class=agent_class,
        target_repo=target_repo_label,
        session_repo=session_repo_label,
        gitdir=marker_gitdir,
        session_id=effective_sid,
        sandbox_root=sandbox_root,
        destination_class=destination_class,
        destination_owner=destination_owner,
        raw_target=raw_target if raw_target and raw_target != target_dir else "",
    )
    return _deny(message)


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------


def check_bump_foreign_repo_write(
    cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """The cross-repo write-confinement bump (C4/C5e). Returns a deny
    envelope (the bump message) when this command's own write/commit
    targets a git root other than the session's own; `None` (allow,
    silently) in every other case, including every unresolvable one --
    see module docstring, § Design posture.

    DIALECT GATE (C5e, 2026-08-07, guard-dialect-coverage.md row 16):
    `Dialect.POWERSHELL` routes to `_check_bump_foreign_repo_write_
    powershell`, which extracts candidates via the PowerShell cmdlet-shaped
    write-sink table (`_write_bump_sink_shapes.PS_WRITE_SINK_CMDLETS`) and
    then judges every candidate through the IDENTICAL predicate this Bash
    body uses (`_evaluate_foreign_repo_candidate`) -- the two legs differ
    ONLY in candidate extraction, never in verdict logic. `Dialect.BASH`/
    `None` falls through unchanged below (AC4).
    """
    dialect = dialect_from_tool_name(payload.get("tool_name") if isinstance(payload, dict) else None)
    if dialect is Dialect.POWERSHELL:
        return _check_bump_foreign_repo_write_powershell(cmd, session_id, cwd, payload)

    if not cmd or not cmd.strip() or not session_id:
        return None

    env = os.environ

    # AC5 -- unconditional, checked before applicability/marker/anything else.
    if _command_invokes_cross_repo_memo(cmd, cwd, env=env):
        return None

    if not bump_applies(session_id, cwd=cwd, env=env):
        return None

    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return None
    anchor_gitdir = resolve_gitdir(anchor)
    anchor_has_repo = anchor_gitdir is not None
    # AC14 -- the COMMON-dir form (worktree-safe), not `anchor_gitdir`'s
    # per-worktree private form, is what `_same_repo_root` compares on.
    anchor_common_cf = (
        _common_dir_cf_from_root(resolve_git_root(anchor)) if anchor_has_repo else None
    )

    agent_id = payload.get("agent_id") or "" if isinstance(payload, dict) else ""

    for target_dir, _label, raw_target in _iter_write_sink_candidates(cmd, cwd):
        # Review: coordinator:code-reviewer -- keyword args at both call
        # sites give this shared eight-parameter predicate a reorder-safe
        # net across the Bash and PowerShell legs.
        result = _evaluate_foreign_repo_candidate(
            target_dir=target_dir,
            session_id=session_id,
            payload=payload,
            anchor=anchor,
            anchor_has_repo=anchor_has_repo,
            anchor_common_cf=anchor_common_cf,
            env=env,
            agent_id=agent_id,
            raw_target=raw_target,
        )
        if result is not None:
            return result

    return None


# ---------------------------------------------------------------------------
# PowerShell-dialect leg (C5e, 2026-08-07, guard-dialect-coverage.md row 16).
# EXTRACTION ONLY differs from the Bash body above -- the PowerShell cmdlet
# table (`PS_WRITE_SINK_CMDLETS`/`extract_write_sink_targets_powershell`)
# replaces `_iter_write_sink_candidates`'s git-subcommand/plain-bash-sink
# scan, but every resulting candidate is judged through the SAME
# `_evaluate_foreign_repo_candidate` this guard's Bash body uses -- this is
# the semantic difference from the sibling `bump_outside_repo_write.py`'s
# own PowerShell leg (which fires on NO git root; this guard fires on a
# DIFFERENT git root -- see this module's own docstring, "WHAT COUNTS AS
# THE SESSION'S OWN REPO", and do not copy that sibling's resolution path).
# Kept as a fully separate function (not interleaved into the bash body),
# matching `block_subagent_destructive_action._check_powershell`'s own
# "kept separate" reasoning for the identical structural reason.
# ---------------------------------------------------------------------------


def _check_bump_foreign_repo_write_powershell(
    cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """PowerShell-dialect leg. Detects ONLY the cmdlet-shaped write-sink
    table `_write_bump_sink_shapes.PS_WRITE_SINK_CMDLETS` names (`New-Item`/
    `Set-Content`/`Add-Content`/`Copy-Item`/`Move-Item`/`Out-File`/
    `Tee-Object`). Every other PowerShell shape in a given command -- a bare
    `>`/`>>` redirect, a `cp`/`mv` alias, an unrecognized cmdlet, `git`
    invoked from PowerShell (this leg does not attempt `-C`/`--git-dir`
    tracking the way the Bash body's `_git_subcommand_and_target_cwd` does),
    or a segment this dialect's tokenizer cannot parse at all -- records
    SILENT and is never treated as "inspected and clean", per this plan's
    "prefer SILENT to a guess" posture (mirrors `bump_outside_repo_write.
    _check_bump_outside_repo_write_powershell`'s own SILENT-on-unmatched-
    shape discipline).

    TRACKS `Set-Location`/`cd`/`sl`/`chdir` ACROSS SEGMENTS (fixed
    2026-08-08, backlog row
    `2026-08-07-bump-foreign-repo-write-s-powershell-leg-3254b856d676`),
    mirroring the bash body's own `effective_cwd` tracking
    (`_iter_write_sink_candidates`'s `cd` branch) rather than inventing a
    second mechanism: a running `effective_cwd` is threaded across
    segments, updated by each resolvable `Set-Location`/alias target
    (`extract_set_location_target_powershell`), so a cmdlet write-target
    candidate extracted AFTER a directory change resolves against the
    changed base, not the original `cwd`.

    SILENT-NOT-DENY IS ABSOLUTE HERE: once a `Set-Location` target fails to
    resolve (`_resolve_relative` returns `None` -- an unresolvable literal,
    a `$var`-shaped or otherwise unexpanded target, same limitation this
    package already carries for `-C $VAR`/`GIT_DIR="$VAR"`), `effective_cwd`
    is left UNTRUSTED for the remainder of this command: every subsequent
    write-sink candidate is declined outright (never judged, never a
    guessed base) rather than resolved against a now-possibly-stale cwd.
    This is a strictly WIDER silence than "just resolve against the last
    known-good cwd" would be -- deliberately, since the whole point of this
    fix is closing false negatives, and continuing to judge candidates
    against a base already known wrong could manufacture either a false
    bump or a false pass off a wrong parent. A `Set-Location` invoked with
    no target at all (bare `cd`, real PowerShell's `$HOME` default this
    module has no reliable way to resolve) is treated as "cwd unchanged",
    never as "cwd now unknown" -- see `extract_set_location_target_
    powershell`'s own docstring.

    Only LITERAL `Set-Location` targets are followed -- a target containing
    `$` (a PowerShell variable, `$env:` reference, or `$(...)`
    subexpression -- `Set-Location $foo`/`Set-Location "$($x)/dir"`) is
    NEVER handed to `_resolve_relative` at all, since composing it onto
    `effective_cwd` would silently manufacture a wrong-but-plausible-
    looking base (the exact "guess a base" this fix must not do, same
    named, accepted limitation as the bash leg's env-override handling,
    `_env_repo_overrides`'s own docstring for why a literal-text
    interpretation is not attempted). It is treated identically to an
    `_resolve_relative`-unresolvable target: `effective_cwd` goes
    untrusted and every later candidate in this command is declined.
    """
    if not cmd or not cmd.strip() or not session_id:
        return None

    env = os.environ

    # AC5 -- unconditional, checked before applicability/marker/anything
    # else, identically to the Bash body above.
    if _command_invokes_cross_repo_memo(cmd, cwd, env=env):
        return None

    if not bump_applies(session_id, cwd=cwd, env=env):
        return None

    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return None
    anchor_gitdir = resolve_gitdir(anchor)
    anchor_has_repo = anchor_gitdir is not None
    anchor_common_cf = (
        _common_dir_cf_from_root(resolve_git_root(anchor)) if anchor_has_repo else None
    )

    agent_id = payload.get("agent_id") or "" if isinstance(payload, dict) else ""

    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name="bump-foreign-repo-write")
    if segments is None:
        # SILENT already recorded by `resolve_segments_for_dialect` itself
        # (ImportError, `has_error` parse residue, absent dialect) -- no
        # second SILENT path needed for that case.
        return None

    effective_cwd = cwd or os.getcwd()
    matched_any_cmdlet = False
    cwd_unresolved = False

    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        head_low = tokens[0].lower()

        if head_low in PS_SET_LOCATION_ALIASES:
            target = extract_set_location_target_powershell(tokens, head_low)
            if target is None:
                # Bare `Set-Location`/`cd` with no argument -- treated as
                # "cwd unchanged", never as "cwd now unknown" (see this
                # function's own docstring).
                continue
            if cwd_unresolved:
                # Already lost track of the base -- nothing this segment
                # resolves against can be trusted either, so skip without
                # re-recording (one SILENT per command is enough signal).
                continue
            if "$" in target:
                # Variable-valued target -- never composed onto
                # `effective_cwd` (would guess a base); go SILENT for the
                # rest of this command instead.
                cwd_unresolved = True
                record_silent(
                    "bump-foreign-repo-write",
                    "PowerShell leg's Set-Location target %r is variable-"
                    "valued -- every subsequent write-sink candidate in "
                    "this command is declined rather than resolved "
                    "against a guessed base (see this function's own "
                    "docstring)" % target,
                )
                continue
            resolved = _resolve_relative(effective_cwd, target)
            if resolved is None:
                cwd_unresolved = True
                record_silent(
                    "bump-foreign-repo-write",
                    "PowerShell leg's Set-Location target %r could not be "
                    "resolved -- every subsequent write-sink candidate in "
                    "this command is declined rather than judged against "
                    "an untrusted base cwd (see this function's own "
                    "docstring)" % target,
                )
            else:
                effective_cwd = resolved
            continue

        raw_targets = extract_write_sink_targets_powershell(tokens, head_low)
        if not raw_targets:
            continue
        matched_any_cmdlet = True

        if cwd_unresolved:
            # The base this segment's candidates would resolve against is
            # untrusted (an earlier Set-Location in this command failed to
            # resolve) -- decline every candidate rather than judge them
            # against a wrong parent (see this function's own docstring).
            continue

        for raw_target in raw_targets:
            target_dir = _resolve_relative(effective_cwd, raw_target)
            if target_dir is None:
                # Untranslatable -- fail open, no verdict for this
                # candidate (this guard family's FAIL OPEN posture).
                continue
            result = _evaluate_foreign_repo_candidate(
                target_dir=target_dir,
                session_id=session_id,
                payload=payload,
                anchor=anchor,
                anchor_has_repo=anchor_has_repo,
                anchor_common_cf=anchor_common_cf,
                env=env,
                agent_id=agent_id,
                raw_target=raw_target,
            )
            if result is not None:
                return result

    if not matched_any_cmdlet and not cwd_unresolved:
        record_silent(
            "bump-foreign-repo-write",
            "PowerShell leg matched no recognized cmdlet write-sink verb "
            "(New-Item/Set-Content/Add-Content/Copy-Item/Move-Item/Out-File/"
            "Tee-Object) -- redirection operators, cp/mv aliases, git "
            "invocations, and any other PowerShell shape are declined "
            "rather than guessed at (see this function's own docstring)",
        )

    return None
