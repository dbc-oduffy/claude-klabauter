"""coordinator_core.bash_guards.bump_foreign_repo_write -- the Bash-surface
CROSS-REPO write-confinement speed bump (C4): a well-meaning session commits
or writes into a git repo other than its own, and nothing today notices.

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567 [DoE-claude
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
  - NOT `block_reviewer_bash_outside_allowlist._GIT_READONLY_SUBCOMMANDS`.
    That constant answers a confinement question (may a confined agent run
    this?) where an unknown verb must DENY; this guard asks "is this a
    write?", where an unknown verb must NOT bump. It was reused inverted
    until 2026-08-12 and billed every read-only verb outside its eight
    names as a write -- see `_GIT_WRITE_SUBCOMMANDS`, this module's own
    bump-by-membership set.
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

READS NEVER BUMP. Only a git WRITE subcommand (i.e. a MEMBER of
`_GIT_WRITE_SUBCOMMANDS`) or a recognised plain-bash write-sink shape is
ever a candidate at all -- see `_iter_write_sink_candidates`. An
unrecognised git verb is not a write and does not bump.

UNRESOLVED IS NOT THE SAME FACT AS REPO-LESS (bug `2026-08-15-anchor-
resolution-misfire`, reproduced live on this guard's tool-surface twin,
`write_guards/bump_out_of_repo_tool_write.py`). Both this guard's Bash body
and its PowerShell leg used to read `anchor_gitdir is None` (i.e.
`anchor_has_repo = False`) as proof the session anchor sits in no git repo
-- but `resolve_gitdir` returns `None` for that fact AND for a `git
rev-parse --git-dir` spawn that simply failed (timeout, missing binary,
transient error), an expected outcome under this box's documented load norm
(50-70 concurrent LLMs, `docs/wiki/machine-load-norm.md`), not an anomaly.
`anchor_has_repo = False` falls into `_evaluate_foreign_repo_candidate`'s
own no-repo-anchor branch, where a REGISTERED target bumps unconditionally
-- so a transient spawn failure could deny a write into the session's OWN
repo whenever that repo happens to be registered. Both call sites now call
`path_has_git_ancestor(anchor)` (`_write_bump_marker.py`, a pure filesystem
walk, no subprocess) immediately after computing `anchor_has_repo`: a
`.git` entry found on that walk is evidence of the SECOND fact, not the
first, and the guard returns `None` (allow) before ever reaching the
candidate loop. A genuinely repo-less anchor (no `.git` entry either) is
unaffected -- it falls through exactly as before, so the 2026-08-10 PM
ruling ("a REGISTERED target still bumps unconditionally from a repo-less
anchor") stays untouched.

Negative-spec:
  - Does NOT add fail-closed behaviour anywhere -- see § Design posture.
  - Does NOT add unforgeability machinery to the marker -- consumes C3's
    marker exactly as written, no creation guard, no identity gating.
  - Does NOT enumerate evasions -- no `-c`-payload shell-token recursion
    (that is C5's AC4, not this guard's), no brace-expansion handling, no
    adversarial interpreter-indirection unwrapping. The one interpreter
    shape this guard DOES read is the accidental one the 2026-08-14
    PM-ratified reversal covers for C5 -- a Python write target living only
    in a heredoc body or a `python`/`python3 -c` payload -- via the same
    shared `extract_interpreter_payload_write_sink_targets`, for parity
    with the sibling guard (2026-09-19, DoE memo
    foreign-write-guard-misses-interpreter-payload). base64, `exec`,
    assembled paths and other interpreters stay out.
  - Does NOT compose a gitdir path -- every gitdir this module touches comes
    from `_write_bump_marker.resolve_gitdir` (`git rev-parse --git-dir`,
    resolved not composed).
  - Does NOT resolve applicability from the live payload `cwd` -- see
    "WHAT COUNTS AS..." above.
  - Does NOT treat `anchor_has_repo is False` as proof the session anchor is
    repo-less without first consulting `path_has_git_ancestor(anchor)` --
    see "UNRESOLVED IS NOT THE SAME FACT AS REPO-LESS" above, both call
    sites. Do not revert either early return back to falling straight into
    `_evaluate_foreign_repo_candidate`'s no-repo-anchor branch; that
    re-opens the exact misfire this fix closes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from coordinator_core.bash_guards._write_bump_stand_down import (
    environment_stands_the_bump_down as _environment_stands_the_bump_down,
    log_environment_stand_down as _shared_log_environment_stand_down,
    stand_down_notice as _stand_down_notice,
)

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
    own_repo_write_gitdir,
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
    path_has_git_ancestor,
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
    extract_interpreter_payload_write_sink_targets,
    extract_set_location_target_powershell,
    extract_write_sink_targets_for_segment,
    extract_write_sink_targets_powershell,
    nearest_existing_ancestor as _nearest_existing_ancestor,
    resolve_relative as _resolve_relative,
)
from coordinator_core.bash_guards.commit_tripwires import _same_tree
from coordinator_core.git.git_dir import common_dir_from_gitdir, resolve_git_common_dir
from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
from coordinator_core.session import machinery_paths
from coordinator_core.trusted_root_guard import _settings_home_dir_from_env
from coordinator_core.write_guards._case_fold_path import casefold_path

_DASH_C_ATTACHED_RE = re.compile(r"^-C(.+)$")

#: twins of the `GIT_DIR`/`GIT_WORK_TREE` env vars below. Real `git`
_DASH_GIT_DIR_ATTACHED_RE = re.compile(r"^--git-dir=(.+)$")
_DASH_WORK_TREE_ATTACHED_RE = re.compile(r"^--work-tree=(.+)$")

#: SUBCOMMAND (an over-block/misclassification bug independent of
_DASH_C_LOWER_ATTACHED_RE = re.compile(r"^-c(.+)$")

#: (`git-config(1)`, `core.worktree`): "The `GIT_WORK_TREE` environment
#: configuration variable" -- i.e. `--work-tree` > `GIT_WORK_TREE` >
_CORE_WORKTREE_CONFIG_RE = re.compile(r"^core\.worktree=(.*)$", re.IGNORECASE)

#: Other git global options that take a MANDATORY value and support both
#: `--exec-path` is DELIBERATELY excluded -- `git(1)` documents it as
#: `--exec-path[=<path>]`, an OPTIONAL argument, which git's own
_CONFIG_ENV_ATTACHED_RE = re.compile(r"^--config-env=(.+)$")
_NAMESPACE_ATTACHED_RE = re.compile(r"^--namespace=(.+)$")
_SUPER_PREFIX_ATTACHED_RE = re.compile(r"^--super-prefix=(.+)$")

_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

#: honours `GIT_DIR`/`GIT_WORK_TREE`/`GIT_COMMON_DIR` (env) and
#: forms) REGARDLESS of `cwd` -- a command that never `cd`s and never
_GIT_DIR_ENV_NAMES = ("GIT_DIR", "GIT_COMMON_DIR")
_WORK_TREE_ENV_NAME = "GIT_WORK_TREE"

#: `block_reviewer_bash_outside_allowlist._GIT_READONLY_SUBCOMMANDS`.
#: That constant is a deny-by-omission CONFINEMENT allowlist: it answers
#: `fetch` is a DUAL-MODE member (see `_fetch_is_read` below): a bare
#: A DUAL-MODE verb -- one whose read and write spellings differ only by
#: `_DUAL_MODE_READ_PREDICATES` below, which vetoes the bump for its
#: `_DUAL_MODE_READ_PREDICATES` entry below.
_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "bisect",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "config",
        "fetch",
        "filter-branch",
        "gc",
        "hash-object",
        "init",
        "merge",
        "mv",
        "notes",
        "pack-refs",
        "prune",
        "pull",
        "push",
        "rebase",
        "reflog",
        "remote",
        "repack",
        "replace",
        "reset",
        "restore",
        "revert",
        "rm",
        "sparse-checkout",
        "stash",
        "submodule",
        "switch",
        "symbolic-ref",
        "tag",
        "update-index",
        "update-ref",
        "worktree",
    }
)


_REDIRECT_TOKEN_RE = re.compile(r"^(?:\d*[<>]|&>)")


def _is_redirect_token(tok: str) -> bool:
    return bool(_REDIRECT_TOKEN_RE.match(tok))


def _first_positional(args: List[str], value_taking: Tuple[str, ...] = ()) -> Optional[str]:
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in value_taking:
            i += 2
            continue
        if tok.startswith("-") or _is_redirect_token(tok):
            i += 1
            continue
        return tok
    return None


def _positionals(args: List[str], value_taking: Tuple[str, ...] = ()) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in value_taking:
            i += 2
            continue
        if tok.startswith("-") or _is_redirect_token(tok):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _flag_present(args: List[str], names: frozenset) -> bool:
    """Whether any token in `args` is one of `names`, matching both the bare
    (`--set-upstream-to`) and `=`-attached (`--set-upstream-to=origin/x`)
    spellings of a long flag. Short bundles (`-dr`) are NOT decomposed -- an
    unrecognised bundle simply fails to match, `True` or `False` either way,
    same as any other unmatched token.

    Review finding (classifier-dual-mode, P2): this docstring previously
    claimed an unmatched token "lands on the write side, the safe direction
    for this whole table" -- that is NOT a property of this helper. It only
    held for `_branch_is_read`/`_tag_is_read` because their write-flag check
    runs before their read-flag check AND every real destructive short flag
    for `branch`/`tag` requires a positional, so an unmatched bundle used to
    fall through to a `_first_positional(args) is None` fallback that still
    landed on write only because a positional was present -- see the P1 fix
    to `_branch_is_read`/`_tag_is_read` below, which now enforces
    fail-toward-write directly rather than relying on this helper's return
    value alone. The fail-toward-write OBLIGATION belongs to each caller,
    not to this token-matching primitive."""
    for tok in args:
        if tok in names:
            return True
        if tok.startswith("--") and "=" in tok and tok.split("=", 1)[0] in names:
            return True
    return False


def _unrecognised_flag_present(
    args: List[str], known: frozenset, extra_patterns: Tuple[re.Pattern, ...] = ()
) -> bool:
    """Whether `args` contains any `-`-prefixed token that is NOT a member of
    `known` (bare or `=`-attached spelling) and does not match any of
    `extra_patterns` (for shapes like `git tag -n5`'s numeric-suffix flag).

    Review finding (classifier-dual-mode, P1): added so a dual-mode
    predicate's flagless-fallback branch (`_first_positional(args) is
    None`) can be gated on "every flag token present is a RECOGNISED read
    flag" rather than silently treating an unparsed/unknown flag bundle as
    absent. `git branch -qXz` (an unrecognised bundle, no positional) must
    land on write, not fall through to the bare-verb read fallback -- see
    `_branch_is_read`/`_tag_is_read`."""
    for tok in args:
        if not tok.startswith("-"):
            continue
        if tok in known:
            continue
        if tok.startswith("--") and "=" in tok and tok.split("=", 1)[0] in known:
            continue
        if any(p.match(tok) for p in extra_patterns):
            continue
        return True
    return False


_BRANCH_WRITE_FLAGS = frozenset(
    {
        "-d", "-D", "--delete",
        "-m", "-M", "--move",
        "-c", "-C", "--copy",
        "-f", "--force",
        "-u", "--set-upstream", "--set-upstream-to", "--unset-upstream",
        "--edit-description",
    }
)

_BRANCH_READ_FLAGS = frozenset(
    {
        "--show-current",
        "-l", "--list",
        "-a", "--all",
        "-r", "--remotes",
        "-v", "-vv", "--verbose",
        "--contains", "--no-contains",
        "--merged", "--no-merged",
        "--points-at",
        "--format", "--sort",
        "-i", "--ignore-case",
        "--color", "--no-color", "--column", "--no-column",
    }
)

_TAG_WRITE_FLAGS = frozenset(
    {
        "-d", "--delete",
        "-a", "--annotate",
        "-s", "--sign", "-u", "--local-user",
        "-m", "--message", "-F", "--file",
        "-f", "--force",
        "--create-reflog",
        "-e", "--edit",
    }
)

_TAG_READ_FLAGS = frozenset(
    {
        "-l", "--list",
        "-n", "--contains", "--no-contains",
        "--points-at", "--merged", "--no-merged",
        "--format", "--sort",
        "-i", "--ignore-case",
        "-v", "--verify",
        "--column", "--no-column",
    }
)

_CONFIG_WRITE_FLAGS = frozenset(
    {
        "--add",
        "--unset", "--unset-all",
        "--replace-all",
        "-e", "--edit",
        "--rename-section", "--remove-section",
    }
)

_CONFIG_READ_FLAGS = frozenset(
    {
        "--get", "--get-all", "--get-regexp", "--get-urlmatch",
        "--get-color", "--get-colorbool",
        "-l", "--list",
    }
)

#: `git config` flags taking a MANDATORY separate-token value that would
_CONFIG_VALUE_TAKING = ("--file", "-f", "--blob", "--type", "-t", "--default")

_CONFIG_WRITE_SUBWORDS = frozenset(
    {"set", "unset", "unset-all", "add", "replace-all", "rename-section", "remove-section", "edit"}
)

_CONFIG_READ_SUBWORDS = frozenset({"get", "list"})

_APPLY_READ_FLAGS = frozenset({"--check", "--stat", "--numstat", "--summary"})

_HASH_OBJECT_WRITE_FLAGS = frozenset({"-w"})

_SYMBOLIC_REF_WRITE_FLAGS = frozenset({"-d", "--delete"})


_TAG_N_SUFFIX_RE = re.compile(r"^-n\d+$")


def _branch_is_read(args: List[str]) -> bool:
    """Review finding (classifier-dual-mode, P1): the flagless fallback
    below (`_first_positional(args) is None`) used to run unconditionally
    once neither the write nor read flag sets matched -- so an UNRECOGNISED
    flag bundle with no trailing positional (`git branch -qXz`) fell through
    to that fallback, found no positional, and returned `True` (read, no
    bump), contradicting this module's own fail-toward-write guarantee. The
    `_unrecognised_flag_present` gate below closes that: the flagless
    fallback now only fires when every `-`-prefixed token present is a
    RECOGNISED read flag (or there are none at all); any unrecognised flag
    token makes the invocation a write. Bare `git branch` (no args at all)
    is unaffected -- no flags to be unrecognised."""
    if _flag_present(args, _BRANCH_WRITE_FLAGS):
        return False
    if _unrecognised_flag_present(args, _BRANCH_WRITE_FLAGS | _BRANCH_READ_FLAGS):
        return False
    if _flag_present(args, _BRANCH_READ_FLAGS):
        return True
    return _first_positional(args) is None


def _tag_is_read(args: List[str]) -> bool:
    if _flag_present(args, _TAG_WRITE_FLAGS):
        return False
    if _unrecognised_flag_present(
        args, _TAG_WRITE_FLAGS | _TAG_READ_FLAGS, extra_patterns=(_TAG_N_SUFFIX_RE,)
    ):
        return False
    if _flag_present(args, _TAG_READ_FLAGS):
        return True
    for tok in args:
        if _TAG_N_SUFFIX_RE.match(tok):
            return True
    return _first_positional(args) is None


def _remote_is_read(args: List[str]) -> bool:
    first = _first_positional(args)
    if first is None:
        return True
    return first in ("show", "get-url")


def _config_is_read(args: List[str]) -> bool:
    if _flag_present(args, _CONFIG_WRITE_FLAGS):
        return False
    positionals = _positionals(args, _CONFIG_VALUE_TAKING)
    if positionals and positionals[0] in _CONFIG_WRITE_SUBWORDS:
        return False
    if positionals and positionals[0] in _CONFIG_READ_SUBWORDS:
        return True
    if _flag_present(args, _CONFIG_READ_FLAGS):
        return True
    return len(positionals) <= 1


def _reflog_is_read(args: List[str]) -> bool:
    first = _first_positional(args)
    if first is None:
        return True
    return first not in ("expire", "delete")


def _notes_is_read(args: List[str]) -> bool:
    first = _first_positional(args)
    if first is None:
        return True
    return first in ("list", "show", "get-ref")


def _stash_is_read(args: List[str]) -> bool:
    first = _first_positional(args)
    return first in ("list", "show")


def _submodule_is_read(args: List[str]) -> bool:
    first = _first_positional(args)
    if first is None:
        return True
    return first in ("status", "summary")


def _bisect_is_read(args: List[str]) -> bool:
    first = _first_positional(args)
    if first is None:
        return False
    return first in ("log", "view", "visualize")


def _worktree_is_read(args: List[str]) -> bool:
    return _first_positional(args) == "list"


def _apply_is_read(args: List[str]) -> bool:
    return _flag_present(args, _APPLY_READ_FLAGS)


_FETCH_REFSPEC_DST_RE = re.compile(r"^\+?[^:\s]+:[^:\s]+$")


def _fetch_is_read(args: List[str]) -> bool:
    return not any(_FETCH_REFSPEC_DST_RE.match(tok) for tok in _positionals(args))


def _hash_object_is_read(args: List[str]) -> bool:
    return not _flag_present(args, _HASH_OBJECT_WRITE_FLAGS)


def _symbolic_ref_is_read(args: List[str]) -> bool:
    if _flag_present(args, _SYMBOLIC_REF_WRITE_FLAGS):
        return False
    return len(_positionals(args)) == 1


#: Dual-mode verbs: in `_GIT_WRITE_SUBCOMMANDS` by default, vetoed by these
_DUAL_MODE_READ_PREDICATES = {
    "apply": _apply_is_read,
    "bisect": _bisect_is_read,
    "branch": _branch_is_read,
    "config": _config_is_read,
    "fetch": _fetch_is_read,
    "hash-object": _hash_object_is_read,
    "notes": _notes_is_read,
    "reflog": _reflog_is_read,
    "remote": _remote_is_read,
    "stash": _stash_is_read,
    "submodule": _submodule_is_read,
    "symbolic-ref": _symbolic_ref_is_read,
    "tag": _tag_is_read,
    "worktree": _worktree_is_read,
}


def _git_invocation_is_write(subcommand: str, args: List[str]) -> bool:
    """Whether `git <subcommand> <args>` MUTATES the target repo.

    Membership in `_GIT_WRITE_SUBCOMMANDS` first (an unknown verb is not a
    write -- "READS NEVER BUMP", module docstring), then the dual-mode
    predicate veto for the verbs whose read and write spellings share a
    name."""
    if subcommand not in _GIT_WRITE_SUBCOMMANDS:
        return False
    predicate = _DUAL_MODE_READ_PREDICATES.get(subcommand)
    if predicate is None:
        return True
    try:
        return not predicate(args)
    except Exception as exc:  # noqa: BLE001 -- fail toward the bump, never crash the dispatcher
        record_silent(
            "bump-foreign-repo-write",
            "dual-mode read predicate for git subcommand %r raised %s: %s "
            "-- treating this invocation as a write (fail toward bump)"
            % (subcommand, type(exc).__name__, exc),
        )
        return True


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


_STAND_DOWN_MARKER = "STAND-DOWN-FOREIGN-REPO-WRITE"
_STAND_DOWN_SINK = "foreign-repo-write.log"


def _log_environment_stand_down(
    git_root: Optional[str], session_id: str, target_repo: str, evidence: str
) -> None:
    _shared_log_environment_stand_down(
        git_root,
        session_id,
        target_repo,
        evidence,
        marker=_STAND_DOWN_MARKER,
        sink_basename=_STAND_DOWN_SINK,
    )


def _resolve_and_casefold(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        return None
    return casefold_path(resolved)


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


#: `bump_out_of_repo_tool_write._LESSONS_OUTBOX_SEGMENTS` exactly (same two
_LESSONS_OUTBOX_SEGMENTS = ("state", "lessons-outbox")


def _target_is_lessons_outbox_write(file_path: str) -> bool:
    """True iff `file_path`'s path components contain `state/lessons-outbox`
    as an adjacent, case-folded directory pair -- the Bash-surface twin of
    `bump_out_of_repo_tool_write._target_is_lessons_outbox_write` (same
    module docstring section there, "LESSONS-OUTBOX IS NOT A MISWRITE, EVEN
    THOUGH IT IS A FOREIGN REPO"). `coordinator-lesson-promote`
    (`ops/queue_promote.py`) writes a universal lesson's durable home to
    `<doe_root>/state/lessons-outbox/<id>.yaml` BY DESIGN -- DoE-claude is
    the central lessons repo, there is no in-repo alternative, and a foreign-
    repo bump on that write is a false positive on both surfaces alike.
    Callers treat `True` as "never bump".

    DELIBERATELY NOT CONJUNCTIVE WITH "NO GIT REPO", unlike this module's
    sibling `_target_is_bare_temp_scratch`-shaped exemptions (see the tool
    surface's own docstring, same section, for why): the whole point of this
    exemption is that the target IS a foreign repo -- `queue.promote` always
    writes into an actual DoE-claude checkout -- so gating on "no repo"
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


def _common_dir_cf_from_gitdir(private_gitdir: Optional[Path]) -> Optional[str]:
    """The resolved+case-folded git COMMON dir for an ALREADY-RESOLVED
    private gitdir -- the spawn-free counterpart of `_common_dir_cf_from_root`,
    and the form both AC14 comparison sides now use.

    PREFER THIS OVER THE ROOT FORM WHEREVER A GITDIR IS IN HAND. The root
    form's argument comes from `resolve_git_root`, a `git rev-parse
    --show-toplevel` spawn documented as failing open to `None` on "not a
    git repo, git missing, transient spawn error"; the transient leg is
    routine under this box's load norm, not exotic. Both callers of this
    predicate already HOLD a gitdir (`resolve_gitdir`, per-process
    memoized) at the point of comparison, so paying that spawn bought
    nothing but a value that could be `None` for reasons having nothing to
    do with repo identity.

    That mattered because BOTH readings of such a `None` are wrong, which is
    why this function exists instead of a rule for interpreting it:
    treating it as "a different repo" denied sessions writes to their OWN
    repo (bug-backlog `2026-08-21-foreign-write-guard-denies-own-repo-push-
    on-unresolved-target-root`, whose tell is a deny naming the same path in
    both message slots); treating it as "allow" was MEASURED to permit a
    genuinely foreign write whenever the ANCHOR's root resolution missed
    while its gitdir resolved -- a state the upstream missing-gitdir early
    return does not cover. Deriving from the gitdir removes the unresolved
    state rather than picking a side on it.

    `None` only when `private_gitdir` is falsy or the realpath+casefold
    itself fails -- neither reachable from a transient spawn miss.
    """
    if not private_gitdir:
        return None
    return _resolve_and_casefold(str(common_dir_from_gitdir(private_gitdir, private_gitdir)))


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
    cmd: str,
    cwd: Optional[str],
    env: Optional[dict] = None,
    *,
    dialect: Optional[Dialect] = None,
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

    `dialect` (bug-backlog 2026-08-18-powershell-text-reaches-the-posix-
    tokeni-ea6ff0baddab): `None` (every pre-existing caller, the Bash leg)
    keeps today's `resolve_command_positions` posix-tokenize path
    byte-for-byte. `Dialect.POWERSHELL` routes through
    `resolve_segments_for_dialect` instead -- the PowerShell leg calling
    this helper must not feed its own raw PowerShell text to the posix
    tokenizer, the same rule `classify_command`'s `dialect` parameter
    already enforces elsewhere in this package.
    """
    if not cmd or not cmd.strip():
        return False
    heads: List[str] = []
    if dialect is Dialect.POWERSHELL:
        segments = resolve_segments_for_dialect(
            cmd, Dialect.POWERSHELL, guard_name="bump-foreign-repo-write.cross-repo-memo"
        )
        if segments is None:
            return False
        heads = [tokens[0] for tokens, _pipe_before in segments if tokens]
    else:
        try:
            resolved_segments = resolve_command_positions(cmd)
        except Exception:  # noqa: BLE001 -- fail open, never let a parse crash reach the dispatcher
            return False
        heads = [rc.tokens[0] for rc in resolved_segments if rc.depth == 0 and rc.tokens]
    canonical = _cross_repo_memo_executable_path(env)
    for head in heads:
        if not token_matches_binary(head, "cross-repo-memo"):
            continue
        if "/" not in head and "\\" not in head:
            return True
        candidate = head
        if not os.path.isabs(candidate):
            candidate = os.path.join(cwd or os.getcwd(), candidate)
        candidate_cf = _resolve_and_casefold(candidate)
        canonical_cf = _resolve_and_casefold(canonical) if canonical else None
        if candidate_cf is not None and canonical_cf is not None and candidate_cf == canonical_cf:
            return True
    return False


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
) -> Tuple[Optional[str], str, List[str]]:
    """`tokens[0]` is already known to be `git` (basename-normalized).
    Returns `(subcommand, target_cwd, subcommand_args)` -- the first
    non-flag token after
    consuming any `-C <dir>`/`-C<dir>` global option(s) (each resolved in
    turn, relative to the PREVIOUS resolved dir, matching real `git -C`
    chaining semantics), and every other leading `-`-prefixed global option
    skipped WITHOUT attempting to know whether it consumes a separate-token
    value of its own (Anti-scope: "do not enumerate evasions" -- the
    fail-open consequence of under-skipping an option's value is that this
    function returns a wrong `subcommand`, which just means the guard
    either misses a real write [never a false bump] or treats an option
    VALUE as the subcommand [which will not match any entry in
    `_GIT_WRITE_SUBCOMMANDS`, so the bump is simply missed -- both arms of
    this imprecision now fail toward silence, never toward a false bump on
    a read]).

    `subcommand_args` is every token AFTER the subcommand, verbatim -- the
    input `_git_invocation_is_write` needs to tell a dual-mode verb's read
    spelling from its write one (`branch --show-current` vs `branch -d x`).
    Empty when no subcommand was found.

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
            if resolved is not None:
                cwd_for_git = (
                    _worktree_root_for_gitdir_override(resolved) if git_dir_override else resolved
                )
        return tok, cwd_for_git, list(tokens[i + 1 :])
    env_git_dir, env_work_tree = (
        _env_repo_overrides(raw_tokens, tokens) if raw_tokens is not None else (None, None)
    )
    git_dir_override = cli_git_dir or env_git_dir
    override = git_dir_override or cli_work_tree or env_work_tree or cli_config_worktree
    if override:
        resolved = _resolve_relative(cwd_for_git, override)
        if resolved is not None:
            cwd_for_git = (
                _worktree_root_for_gitdir_override(resolved) if git_dir_override else resolved
            )
    return None, cwd_for_git, []


def _is_expansion_valued(target: str) -> bool:
    """A path whose value only the shell knows (`$S/x.log`, `` `pwd`/x ``).

    NEGATIVE SPEC -- never composed onto a cwd: `$S/install2.log` after
    `cd <mirror>` joined to `<mirror>/$S/install2.log` and bumped a write that
    actually lands in `$S`. Unknowable means no verdict (FAIL OPEN); the bare
    `$D` unset-variable shape is `bump_outside_repo_write`'s, not this guard's."""
    return "$" in target or "`" in target


def _iter_write_sink_candidates(
    cmd: str, cwd: Optional[str]
) -> Iterator[Tuple[str, str, Optional[str]]]:
    """Yield `(target_dir, label, raw_target)` for every depth-0 segment of
    `cmd` that is either a git WRITE subcommand (`git -C <dir> <sub>` / `cd
    <dir> && git <sub>`, tracked via a running `effective_cwd` that a `cd`
    segment updates for later segments in the SAME compound command) or a
    recognised plain-bash write-sink shape (`_write_bump_sink_shapes`).

    `target_dir` is NOT YET resolved to a git root -- callers do that via
    `resolve_gitdir`. Only a git subcommand in `_GIT_WRITE_SUBCOMMANDS` is
    yielded from the git leg; a read -- or any verb not on that set -- is
    never yielded at all (module docstring, "READS NEVER BUMP").

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
    is skipped, matching this chunk's own scope (`-c`-payload shell-token
    classification is C5's AC4, not C4's).

    ALSO yields every candidate `extract_interpreter_payload_write_sink_
    targets` finds in `cmd`'s heredoc bodies and `python`/`python3 -c`
    payloads, labelled `interpreter-payload` and resolved against the
    starting cwd, never a `cd`-tracked one -- identical to
    `bump_outside_repo_write._iter_write_sink_candidates`, whose docstring
    carries the rationale.

    Negative-spec: `preserve_windows_backslashes` makes this guard rule on
    the TYPED path, not the EXECUTED one -- deliberate. Modelling execution
    instead would mean ALLOWING an unquoted-backslash write and leaving the
    operator a silent junk file (Git Bash consumes an unquoted backslash
    exactly as `shlex` does, per state/audits/2026-08-07-bash-guard-
    tokenizer-eats-windows-path-separators.md's measured probes -- `echo
    probe > sub\\dir\\out.txt` lands as `subdirout.txt` in cwd, never at the
    typed path). The Bash-leg tokenizer mangling this flag reverses is NOT
    otherwise a defect for THIS tool's executor, for the identical reason:
    Git Bash mangles the same unquoted backslashes the same way. THIS
    module does not itself render that landing name in its denial text --
    its `target_repo` display label is the foreign repo's own ROOT
    (`probe_root or target_dir`), never the typed file path, so naming a
    file-shaped landing name against a directory-shaped label would assert
    a false correspondence. `bump_outside_repo_write.py`'s own
    `_iter_write_sink_candidates` docstring carries the identical rationale
    at the one call site where `target_repo` IS the typed path, and its
    `_no_git_repo_target_label` is the renderer.
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
    payload_base_cwd = effective_cwd
    cwd_unresolved = False
    for rc in resolved_segments:
        if rc.depth != 0:
            continue
        if rc.confidence == ResolutionConfidence.UNRESOLVED or not rc.tokens:
            continue
        head_base = normalize_executable_basename(rc.tokens[0])

        if head_base == "cd":
            positional = [t for t in rc.tokens[1:] if not t.startswith("-")]
            if len(positional) == 1:
                if _is_expansion_valued(positional[0]):
                    cwd_unresolved = True
                    continue
                resolved = _resolve_relative(effective_cwd, positional[0])
                if resolved is not None:
                    effective_cwd = resolved
            continue

        if cwd_unresolved:
            continue

        if head_base == "git":
            subcommand, target_cwd, sub_args = _git_subcommand_and_target_cwd(
                rc.tokens, effective_cwd, raw_tokens=rc.raw_tokens
            )
            if subcommand and _git_invocation_is_write(subcommand, sub_args):
                yield (target_cwd, "git %s" % subcommand, None)
            continue

        for raw_target in extract_write_sink_targets_for_segment(rc.tokens, head_base):
            if _is_expansion_valued(raw_target):
                continue
            resolved_target = _resolve_relative(effective_cwd, raw_target)
            if resolved_target is None:
                continue
            yield (resolved_target, head_base, raw_target)

    for raw_target in extract_interpreter_payload_write_sink_targets(cmd):
        resolved_target = _resolve_relative(payload_base_cwd, raw_target)
        if resolved_target is None:
            continue
        yield (resolved_target, "interpreter-payload", raw_target)


def _sandbox_root_hint(git_root: Optional[str], session_id: str) -> str:
    if not git_root or not session_id:
        return ""
    return machinery_paths.share_dir(git_root, session_id)


# The shared per-candidate JUDGMENT (AC9 -- the thing this leg must NOT
# and can only ever differ in how a candidate `target_dir` is EXTRACTED --


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
    anchor_root: Optional[str] = None,
    write_verb_label: str = "",
    cwd: Optional[str] = None,
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
    display form (AC2's "never print the same string twice" instruction).

    `anchor_root` (AC7 fix, 2026-08-11) is the SESSION's own resolved git
    root (`resolve_git_root(anchor)`), computed ONCE by the caller and
    threaded through -- see "TWO ROOTS, NEVER CONFLATED" below for why this
    must never be `marker_probe_root` (the TARGET's root, computed further
    down in this function for the marker's own siting).

    `write_verb_label` (2026-08-14, percolate-push memo) is the caller's own
    `label` from `_iter_write_sink_candidates` -- `"git push"` for a git-push
    candidate, the executable basename for a plain write-sink one. Threaded
    straight through to `render_bump_message`, which only acts on it for the
    PUBLISH destination class (see `_write_bump_message._GIT_PUSH_WRITE_VERB_LABEL`).

    `cwd` (coordinator-claude#42 B2) is the caller's own command `cwd` --
    consulted ONLY in the no-repo-anchor (`not anchor_has_repo`) branch
    below, as `_write_bump_applicability.own_repo_write_gitdir`'s primary
    candidate, so a REGISTERED target that is also this command's own
    working repo never bumps merely because the session-start anchor
    itself resolved to no git repo. See that helper's own docstring for why
    this is not a second route to the forbidden live-`cwd`-as-anchor
    shape."""
    probe_dir = _nearest_existing_ancestor(target_dir)
    if probe_dir is None:
        return None
    target_gitdir = resolve_gitdir(probe_dir)
    if target_gitdir is None:
        return None

    # Lessons-outbox exemption -- a DIFFERENT axis from the AC5
    # function ever runs). This exemption matches on DESTINATION PATH
    # is known to be non-`None`. It also sits per-CANDIDATE rather than
    if _target_is_lessons_outbox_write(target_dir):
        return None

    if is_agent_memory_store_path(target_dir):
        return None

    if target_is_under_claude_home(target_dir):
        return None

    # _deny(message)`. The AC14 SAME-REPO comparison above already
    # is the "MISS-MODE CALLERS ONLY" shape `resolve_git_root_cheap`
    # docstring names this module's SAME-REPO comparison (a different
    probe_root = _show_toplevel(probe_dir)

    if anchor_has_repo:
        target_common_cf = _common_dir_cf_from_gitdir(target_gitdir)
        if (
            anchor_common_cf is not None
            and target_common_cf is not None
            and _same_repo_root(anchor_common_cf, target_common_cf)
        ):
            return None
        marker_probe = probe_dir
    else:
        # REGISTERED target still bumps unconditionally, as it always has
        # 2026-08-10): an UNREGISTERED target now ALSO bumps unless it
        own_repo_cwd_gitdir = own_repo_write_gitdir(cwd, payload, env=env)
        own_repo_cwd_common_cf = (
            _common_dir_cf_from_gitdir(own_repo_cwd_gitdir)
            if own_repo_cwd_gitdir is not None
            else None
        )
        target_common_cf_for_own_repo = _common_dir_cf_from_gitdir(target_gitdir)
        if (
            own_repo_cwd_common_cf is not None
            and target_common_cf_for_own_repo is not None
            and _same_repo_root(own_repo_cwd_common_cf, target_common_cf_for_own_repo)
        ):
            return None
        if not target_is_registered_repo(
            str(target_gitdir), env=env
        ) and anchor_subtree_contains(anchor, target_dir):
            return None
        # with this as `cwd`, which requires an EXISTING directory.
        marker_probe = probe_dir

    # TWO ROOTS, NEVER CONFLATED (AC7 fix, 2026-08-11, bug
    # SHORT-CIRCUITED, not merely named as a fallback (2026-08-21, this
    session_root_for_marker = (
        anchor_root if anchor_root is not None else resolve_git_root(marker_probe)
    )
    if bump_is_cleared(
        marker_probe, session_id, git_root=session_root_for_marker, agent_id=agent_id
    ):
        return None

    marker_gitdir = resolve_gitdir(marker_probe)
    if marker_gitdir is None or not marker_gitdir_is_writable(marker_gitdir):
        # gitdir exists but is not writable/readable (STAFF-ENG F0, AC5)
        return None

    agent_class = resolve_agent_class(payload if isinstance(payload, dict) else {}, session_root_for_marker)
    effective_sid = effective_session_id(session_id, session_root_for_marker, agent_id)
    sandbox_root = (
        _sandbox_root_hint(session_root_for_marker, effective_sid)
        if agent_class == AGENT_CLASS_SUBAGENT
        else ""
    )

    target_repo_label = probe_root or target_dir
    session_repo_label = resolve_git_root(anchor) or anchor

    # OUTSIDE_ANY_REPO is never reached here, since this guard only ever
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

    # OPERATOR writes the marker via a deliberate `touch` (an
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
        write_verb_label=write_verb_label,
    )
    stood_down = _environment_stands_the_bump_down(env)
    if stood_down is not None:
        _log_environment_stand_down(
            anchor_root, effective_sid, target_repo_label, stood_down.evidence
        )
        _stand_down_notice(
            message
            + "\n\nSTOOD DOWN, NOT ENFORCED — this write is proceeding.\n"
            + "  Why: %s\n" % stood_down.evidence
            + "  This bump protects another team's tree from a session that "
            "shares a machine with them.\n"
            "  Neither is true here, and the alternative it would send you to "
            "(a cross-repo memo)\n"
            "  has no reader on this host either — so enforcing it would "
            "forbid the only correct\n"
            "  move available. The boundary itself is unchanged: a cross-repo "
            "commit still needs\n"
            "  per-session PM assent, and this stand-down is recorded in this "
            "session's overrides.log.\n"
            "  If this host DOES carry a fleet, say so with "
            "COORDINATOR_CAP_FLEET_PRESENT=1 and the bump enforces again."
        )
        return None
    return _deny(message)


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

    APPLICABILITY BEFORE ROOT RESOLUTION (C3, docs/plans/2026-08-21-guards-
    under-the-brightline.md): every write-sink candidate this command
    carries is extracted from the PARSED command alone
    (`_iter_write_sink_candidates` never spawns a subprocess) BEFORE
    `resolve_gitdir(anchor)`/`resolve_git_root(anchor)` ever runs. A command
    with no candidate at all (`echo hello`, `git status --short`) returns
    `None` before either spawn; a command that does carry a write sink pays
    the identical spawns it always did.
    """
    dialect = dialect_from_tool_name(payload.get("tool_name") if isinstance(payload, dict) else None)
    if dialect is Dialect.POWERSHELL:
        return _check_bump_foreign_repo_write_powershell(cmd, session_id, cwd, payload)

    if not cmd or not cmd.strip() or not session_id:
        return None

    env = os.environ

    if _command_invokes_cross_repo_memo(cmd, cwd, env=env):
        return None

    if not bump_applies(session_id, cwd=cwd, env=env):
        return None

    # APPLICABILITY BEFORE ROOT RESOLUTION (C3) -- decide from the parsed
    candidates = list(_iter_write_sink_candidates(cmd, cwd))
    if not candidates:
        return None

    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return None
    anchor_gitdir = resolve_gitdir(anchor)
    anchor_has_repo = anchor_gitdir is not None
    if not anchor_has_repo and path_has_git_ancestor(anchor):
        # UNRESOLVED, not repo-less -- `resolve_gitdir` returning `None`
        # `else` clause below still bumps a REGISTERED target for,
        # first -- treated as UNRESOLVED and allowed, matching this
        return None
    # CONFLATED" docstring section).
    anchor_root = resolve_git_root(anchor) if anchor_has_repo else None
    anchor_common_cf = (
        _common_dir_cf_from_gitdir(anchor_gitdir) if anchor_has_repo else None
    )

    agent_id = payload.get("agent_id") or "" if isinstance(payload, dict) else ""

    for target_dir, write_verb_label, raw_target in candidates:
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
            anchor_root=anchor_root,
            write_verb_label=write_verb_label,
            cwd=cwd,
        )
        if result is not None:
            return result

    return None


# EXTRACTION ONLY differs from the Bash body above -- the PowerShell cmdlet
# table (`PS_WRITE_SINK_CMDLETS`/`extract_write_sink_targets_powershell`)
# DIFFERENT git root -- see this module's own docstring, "WHAT COUNTS AS


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

    if _command_invokes_cross_repo_memo(cmd, cwd, env=env, dialect=Dialect.POWERSHELL):
        return None

    if not bump_applies(session_id, cwd=cwd, env=env):
        return None

    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name="bump-foreign-repo-write")
    if segments is None:
        return None

    effective_cwd = cwd or os.getcwd()
    matched_any_cmdlet = False
    cwd_unresolved = False
    # APPLICABILITY BEFORE ROOT RESOLUTION (C3) -- collected here, from the
    candidates: List[Tuple[str, str]] = []

    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        head_low = tokens[0].lower()

        if head_low in PS_SET_LOCATION_ALIASES:
            target = extract_set_location_target_powershell(tokens, head_low)
            if target is None:
                continue
            if cwd_unresolved:
                continue
            if "$" in target:
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
            continue

        for raw_target in raw_targets:
            if _is_expansion_valued(raw_target):
                continue
            target_dir = _resolve_relative(effective_cwd, raw_target)
            if target_dir is None:
                continue
            candidates.append((target_dir, raw_target))

    if not matched_any_cmdlet and not cwd_unresolved:
        record_silent(
            "bump-foreign-repo-write",
            "PowerShell leg matched no recognized cmdlet write-sink verb "
            "(New-Item/Set-Content/Add-Content/Copy-Item/Move-Item/Out-File/"
            "Tee-Object) -- redirection operators, cp/mv aliases, git "
            "invocations, and any other PowerShell shape are declined "
            "rather than guessed at (see this function's own docstring)",
        )

    if not candidates:
        return None

    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return None
    anchor_gitdir = resolve_gitdir(anchor)
    anchor_has_repo = anchor_gitdir is not None
    if not anchor_has_repo and path_has_git_ancestor(anchor):
        # UNRESOLVED, not repo-less -- see the Bash body's own identical
        return None
    anchor_root = resolve_git_root(anchor) if anchor_has_repo else None
    anchor_common_cf = (
        _common_dir_cf_from_gitdir(anchor_gitdir) if anchor_has_repo else None
    )

    agent_id = payload.get("agent_id") or "" if isinstance(payload, dict) else ""

    for target_dir, raw_target in candidates:
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
            anchor_root=anchor_root,
            cwd=cwd,
        )
        if result is not None:
            return result

    return None
