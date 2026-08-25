"""coordinator_core.bash_guards._write_bump_applicability -- the single entry
point all three write-confinement bump guards (`bump_foreign_repo_write.py`
[C4], `bump_outside_repo_write.py` [C5], `write_guards.bump_out_of_repo_tool_write`
[C7]) consult before deciding whether to bump at all.

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567 [DoE-claude
repo], chunk C2, "Applicability -- anchor on a value agents cannot move,
resolve the two no-bump conditions".

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY. Read the plan's "Design
posture -- passable by construction" section before touching this module.
FAIL OPEN, EVERYWHERE, IS THE WHOLE POINT of this module -- the opposite of
this package's fail-closed neighbours (`_blanket_disarm.py`,
`_sentinel_creation_guard.py`). Every branch below that cannot resolve an
anchor, cannot read the registry, or cannot find a session record returns
"does not apply" (no bump). A bump that misfires on legitimate work gets
disabled, and a disabled guard prevents nothing -- so uncertainty here always
resolves toward NOT bumping, never toward bumping. Do not "fix" this back
toward fail-closed; that undoes the one property the plan asked for.

WHAT THIS MODULE ANSWERS -- exactly two questions, per the plan's own
framing, nothing more:

  1. Does the bump apply at all to this session? (`bump_applies`) -- False
     when the session's anchored launch root sits under `~/.claude` (the
     fleet-recovery hatch: an EM bricked large parts of the fleet once, and
     the tool that repairs that must not itself be obstructed by it), or
     when the anchor cannot be resolved at all.
  2. When the session's own anchored launch root is in no git repo, is a
     given cross-repo commit TARGET a registered repo? (`target_is_registered_repo`)
     -- per the plan's "Where the bump does not fire": a session launched
     to scaffold a brand-new project writes freely into it and into the
     settings home, but committing into a repo the operator has already
     registered in the machine-local registry still bumps.

ANCHORING -- WHY NOT THE LIVE PAYLOAD `cwd`. The harness Bash tool contract
states "Working directory persists between calls" -- an ordinary `cd` in one
tool call leaves every later call reporting the NEW location, with no
adversarial intent required. Anchoring on the live payload `cwd` would make
this module's verdict flip on an incidental `cd`, defeating AC12. Anchoring
instead on a value the session recorded ONCE, at launch, and that an
in-session `cd` cannot move.

WHY "RESOLVE ONCE" HAS NO REFERENT HERE. Every bash-guard hook invocation is
a fresh, stateless, per-call subprocess -- there is no long-lived guard
object whose field could cache "the anchor" across calls the way an
in-process singleton would. "Resolve once, reuse forever" is not something
this module can DO; the only way that property can exist at all is if
something OUTSIDE this process's lifetime persists the value and this
module re-reads it, cheaply, on every call. That something is C0's
SessionStart record (`_write_bump_session_start.py`) -- written once, at
the one moment in a session's life that precedes every `cd` a later Bash
call could perform, and read back here on every guard invocation regardless
of which subprocess is doing the reading. A future reader tempted to
"simplify" this into a module-level cache should stop here: a module-level
cache does not survive process exit, and every guard invocation IS a new
process.

TWO ANCHORS, NOT TWO EQUAL ANCHORS. C0's own liveness probe (see
`_write_bump_session_start.CLAUDE_PROJECT_DIR_LIVE_IN_HOOK_ENV`, currently
pinned `False`) found `CLAUDE_PROJECT_DIR` ABSENT from a live confined
Bash-tool subprocess's environment -- the closest first-party evidence
available without harness-introspection tooling this dispatch does not
have. Consequence, stated exactly as the plan requires: C0's session-start
record is the PRIMARY anchor, not mere corroboration of a live
`CLAUDE_PROJECT_DIR`. `CLAUDE_PROJECT_DIR` is consulted here ONLY as a
fallback for the (currently unobserved) case where a future harness change
makes it live -- if that ever happens, `CLAUDE_PROJECT_DIR_LIVE_IN_HOOK_ENV`
must flip to `True` in the SAME change that promotes this fallback, so the
two modules never disagree about which anchor is primary.

RESOLUTION PRIMITIVES -- REUSED, NEVER REIMPLEMENTED.
  - `~/.claude` is resolved via `trusted_root_guard._home_from_env` (the
    `CLAUDE_HOME` -> `HOME` -> `USERPROFILE` precedence that module already
    carries, including its Windows-without-`HOME`-set fix) rather than a
    literal `os.path.join(os.environ["HOME"], ".claude")`. This module does
    NOT call `trusted_root_guard.is_trusted()` -- that predicate also trusts
    the DoE-claude and project-makima anchors, which would wrongly hand the
    fleet-recovery hatch to every session launched in either of THIS
    plan's own two working repos. Only the home-resolution helper is
    reused; the trust decision itself is this module's own, narrower one.
  - The registered-repos registry lookup goes through
    `machine_resolver._load_toml` / `_flatten` over EVERY `repos.*` entry
    in the machine-local registry (`registry.local.toml` takes precedence
    over `registry.toml`, matching every other reader in this package) --
    not the two named keys (`repos.doe_claude`, `repos.project_makima`)
    `trusted_root_guard.py` reads. Those two are a fixed pair for a fixed
    trust decision; this module's job is "is the target ANY repo the
    operator has registered", which needs the full, open-ended set.
  - The publish-destination registry lookup (`_all_publish_destinations`,
    consumed by `target_is_publish_destination` / `publish_destination_owner`)
    mirrors the `repos.*` lookup exactly, over the disjoint `publish.mirrors.*`
    namespace instead -- same merge order, same `.path`-only membership
    discipline, `.owner` carried alongside for context only. C1
    (docs/plans/2026-08-03-narrow-write-confinement-bump.md).

PATH COMPARISON -- BOTH OPERANDS RESOLVED AND CASE-FOLDED, per the plan's
Anti-scope: `os.path.realpath` on both sides (DR-148's realpath ban is
bash/coreutils portability and is superseded by DR-042 for this plan, per
Zoli F11 -- this module is Python, not bash), then
`write_guards._case_fold_path.casefold_path` on both resolved strings
before the prefix test. Resolving only one operand false-classifies a
session or target reached through a symlink. `casefold_path` itself strips
a Windows extended-length prefix (plain or UNC form) as part of its
normalize -> strip -> casefold pipeline (AC11 / drift D9, generalized
repo-wide by `state/handoffs/2026-08-03-windows-extended-length-prefix-
desync.md`) -- but `_resolve_path` (below) ALSO strips the prefix
explicitly, once more, BEFORE `os.path.realpath` runs (via the same
shared `strip_extended_length_prefix`, not a second implementation of
it): on a POSIX host `realpath` does not recognize a backslash-prefixed
Windows string as absolute and joins it onto cwd instead, which moves the
prefix off the string's head and defeats a strip that only runs AFTER
resolution. The dual strip is deliberate, not redundant -- see
`_resolve_path`'s own docstring for the full argument. Review: code-reviewer
cb2c4bcd, Finding 1 -- module docstring previously said "no longer strips
it separately", contradicting `_resolve_path`'s explicit pre-realpath
strip three lines below it.

OBSERVABILITY (AC18) -- this plan's only evidence channel for the PM's
"prevent 95% of missteps" claim being checkable in a month rather than a
shrug. Whenever `bump_applies` (or a caller acting on its verdict) resolves
"applies", the calling guard SHOULD call `record_applicability_event` to
append one line -- repo, target, session, agent class -- to a small,
append-only per-session log alongside C0's own record. This is
observation, not enforcement: a failed append (unwritable disk, missing
session hub, a race) NEVER blocks the write it is merely logging, and never
raises.

Negative-spec:
  - Does NOT resolve or compare against the live payload `cwd` -- see
    "ANCHORING" above. Every function here takes an explicit `cwd` only to
    locate the *in-repo* session hub (mirroring
    `_write_bump_session_start.read_session_start_record`'s own `cwd`
    parameter, which exists for the same reason). That in-repo hub MOVES
    when `cwd` crosses a repo boundary -- `sessions_dir(cwd)` resolves a
    different repo's hub entirely, where this session's record was never
    written. That is exactly why `resolve_launch_anchor` does not rely on
    the in-repo hub alone: `read_session_start_record` reads the
    settings-home anchor (cwd-INDEPENDENT by construction) first, falling
    back to the in-repo hub only when the settings-home read is empty. See
    `_write_bump_session_start.read_session_start_record`'s own docstring
    for the corrected argument.
  - Does NOT add a bounded-age expiry to anything it reads or writes here
    -- matches `_write_bump_marker.py` / `_write_bump_session_start.py`'s
    own "no read-path expiry" posture; irrelevant to this module's scope,
    stated here only so a later reader does not import one by analogy.
  - Does NOT raise from any public function. Every failure mode collapses
    to a `bool`/`Optional[str]`/no-op, per the module docstring's opening
    paragraph.
  - Does NOT call `trusted_root_guard.is_trusted()` -- see "RESOLUTION
    PRIMITIVES" above for why that predicate is too broad for this
    module's narrower `~/.claude`-only hatch check.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.bash_guards._write_bump_marker import resolve_gitdir
from coordinator_core.bash_guards._write_bump_session_start import (
    read_session_start_record,
)
from coordinator_core.bash_guards._write_bump_sink_shapes import (
    nearest_existing_ancestor,
)
from coordinator_core.machine_resolver import _flatten, _load_toml, registry_dir
from coordinator_core.session.core import now_iso, session_dir
from coordinator_core.trusted_root_guard import _home_from_env
from coordinator_core.write_guards._case_fold_path import (
    casefold_path,
    strip_extended_length_prefix,
)

# `guard_memory_store_cap` (used only inside `is_agent_memory_store_path` below)
# is NOT imported at module scope -- it pulls in `coordinator_core.ops._path_guard`,
# which forces `coordinator_core.ops.__init__`'s `_eager_import_all()` to run,
# which in turn eagerly imports `coordinator_core.hooks` -- see
# `test_bash_guards_avoid_hooks_package.py` and `block_subagent_commit.py`'s own
# "Do NOT import from `coordinator_core.hooks`" note. Deferred to a function-local
# import so merely importing this module (or evaluating a command that never
# reaches `is_agent_memory_store_path`) never pays that cost.

#: Prefix every enumerated registry key must carry to be treated as a
#: registered-repo entry -- mirrors `repos.doe_claude` / `repos.project_makima`
#: shape but widened, per this module's docstring, to ALL `repos.*` keys
#: rather than those two named ones.
# Generator-provenance declaration (generator_provenance.py).
# record_applicability_event appends to session_dir(...)/write_bump_
# applicability_log, under .git/coordinator-sessions/ -- untracked
# session-scoped state, never a tracked repo artifact.
GENERATES = []

_REGISTRY_REPOS_PREFIX = "repos."

#: Prefix every enumerated registry key must carry to be treated as a
#: publish-destination entry -- `publish.mirrors.<name>.path` /
#: `publish.mirrors.<name>.owner`, disjoint from `_REGISTRY_REPOS_PREFIX` by
#: construction (verified at HEAD -- see plan's § Design). C1
#: (docs/plans/2026-08-03-narrow-write-confinement-bump.md).
_REGISTRY_PUBLISH_MIRRORS_PREFIX = "publish.mirrors."

#: Value-key suffixes under a `publish.mirrors.<name>.*` entry. The ROOTS
#: reader (`_all_publish_destinations`) keys on `.path` ONLY, never
#: `.owner` -- membership decides the verdict, `owner` only informs C2's
#: copy (plan § Design, § Anti-scope: "Do not gate the verdict on
#: `owner`").
_PUBLISH_PATH_SUFFIX = ".path"
_PUBLISH_OWNER_SUFFIX = ".owner"

#: Append-only observability log filename (AC18), sibling to C0's own
#: single-value `write_bump_launch_cwd` record under the same per-session
#: directory. Deliberately a SEPARATE file: C0's record is a single scalar
#: overwritten idempotently at SessionStart, and appending log lines into
#: it would break that single-value contract for every reader of this
#: package's other modules.
_APPLICABILITY_LOG_FILENAME = "write_bump_applicability_log"


#: AC11 / merged-in DoE source numbering C9 (see plan's § "Substrate
#: re-verification" and drift D9) landed the extended-length-prefix strip
#: as a private helper scoped to THIS module's own comparison only ("not a
#: repo-wide sweep ... that sweep is the sibling anchor plan's own proposed
#: standalone plan to own"). `state/handoffs/2026-08-03-windows-extended-
#: length-prefix-desync.md` is that sweep: the strip now lives once in
#: `write_guards._case_fold_path.strip_extended_length_prefix`, folded into
#: `casefold_path` itself. This name stays as a re-export (not a second
#: implementation) so existing call sites/tests here are unaffected. It
#: exists SOLELY for AC11 test back-compat (tests reference
#: `applicability._strip_windows_extended_length_prefix` by this name) --
#: production code always uses `strip_extended_length_prefix` directly (see
#: `_resolve_path` below). A future cleanup pass renaming the tests can
#: drop this alias. Review: code-reviewer cb2c4bcd, Finding 3.
_strip_windows_extended_length_prefix = strip_extended_length_prefix


def _resolve_path(raw: str) -> Optional[str]:
    """`os.path.realpath` -> `casefold_path`, or `None` on any resolution
    failure. Both operands of every comparison in this module go through
    this exact helper -- see module docstring, "PATH COMPARISON".

    The raw string is ALSO stripped of a Windows extended-length prefix
    BEFORE `os.path.realpath` runs (via the shared `strip_extended_length_
    prefix`, not a second implementation of it) -- not merely relying on
    `casefold_path`'s own post-resolve strip. On a POSIX test/dev host,
    `os.path.realpath` does not recognize a backslash-prefixed Windows path
    as absolute and joins it onto the current directory instead, which
    moves the prefix away from the string's start and defeats a strip that
    only runs AFTER resolution -- pre-stripping keeps the prefixed and bare
    forms textually identical going INTO `realpath`, so they resolve
    identically regardless of host. `casefold_path`'s own post-resolve
    strip still runs too (idempotent, defensive) for the real-Windows case
    where `realpath` itself is the one that adds the prefix."""
    if not raw:
        return None
    raw = strip_extended_length_prefix(raw)
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        return None
    return casefold_path(resolved)


def _is_under(candidate_cf: str, parent_cf: str) -> bool:
    """Case-folded prefix containment: `candidate_cf` equals `parent_cf`
    or sits under it. Both inputs are ALREADY resolved+case-folded by
    `_resolve_path` -- this helper does no I/O itself."""
    parent_stripped = parent_cf.rstrip("/")
    return candidate_cf == parent_stripped or candidate_cf.startswith(parent_stripped + "/")


#: Public alias. This helper now has a cross-package consumer
#: (`write_guards.bump_out_of_repo_tool_write` imports it directly, not
#: merely a sibling in this same package) -- `_is_under` stays as an alias
#: so that existing `from ..._write_bump_applicability import _is_under`
#: call sites (bash_guards' own C4/C5 modules) do not need to change.
is_under = _is_under


def _posix_tmp_literal() -> str:
    """The `/tmp` literal `_all_temp_roots` resolves on POSIX -- broken out
    as its own one-line function, rather than inlined, purely so a test can
    monkeypatch THIS ONE SEAM to point somewhere other than the real `/tmp`
    without touching `tempfile.gettempdir()` (which a caller may need to
    patch independently for its own, unrelated isolation need). On a
    platform where pytest's own `tmp_path` fixture defaults to living
    directly under the real system temp root (e.g. Linux, where
    `tempfile.gettempdir()` is `/tmp` by default with no `TMPDIR` set), a
    test built entirely from `tmp_path` subdirectories would otherwise
    always resolve under this literal, regardless of any `gettempdir()`
    patch -- this seam is what lets such a test isolate itself. Production
    callers never patch this; it always returns the literal `"/tmp"`."""
    return "/tmp"


def _all_temp_roots(env: Optional[dict] = None) -> list:
    """Every recognized temp root -- resolved, case-folded, deduplicated --
    a bare scratch path might resolve under. See `target_is_bare_temp_scratch`
    for why this is a SET rather than the single `tempfile.gettempdir()`
    value: on macOS, `TMPDIR` (what `gettempdir()` reports) points into
    `/var/folders/...`, but the harness-designated per-session scratchpad
    lives under `/private/tmp` -- a path `gettempdir()` alone never covers.
    `os.path.realpath("/tmp")` closes that gap on POSIX (`/tmp` ->
    `/private/tmp` via the macOS symlink); on Windows `/tmp` simply will not
    resolve to anything meaningful and is harmlessly deduplicated away or
    left unmatched -- not special-cased out, per this fix's own design note.
    `TMPDIR`/`TEMP`/`TMP` are also consulted directly (not merely via
    `gettempdir()`) since a caller may have set one without it being the
    value `tempfile` itself would report.
    """
    env = os.environ if env is None else env
    raw_candidates = []
    try:
        raw_candidates.append(tempfile.gettempdir())
    except OSError:
        pass
    raw_candidates.append(_posix_tmp_literal())
    for var in ("TMPDIR", "TEMP", "TMP"):
        val = env.get(var)
        if val:
            raw_candidates.append(val)
    resolved = []
    seen = set()
    for raw in raw_candidates:
        rp = _resolve_path(raw)
        if rp is None or rp in seen:
            continue
        seen.add(rp)
        resolved.append(rp)
    return resolved


def target_is_bare_temp_scratch(path: str, env: Optional[dict] = None) -> bool:
    """True only when BOTH: `path` resolves under ANY recognized temp root
    (`_all_temp_roots`), AND `path` resolves to NO git repo at all.

    THE CONJUNCTION IS LOAD-BEARING AND MUST NOT BE WEAKENED. A real
    checkout that happens to live under the temp root IS a foreign repo and
    must still bump; an unconditional temp exemption would open a hole the
    size of `git clone $TMPDIR/...`. This predicate answers "is this a bare
    scratch path with nothing else at stake", never "is this merely under
    temp".

    Single shared classifier for both write-confinement bump surfaces (the
    Bash-surface `bump_outside_repo_write.py` [C5] and the tool-surface
    `write_guards.bump_out_of_repo_tool_write` [C7]) -- previously each
    surface derived its own notion of "system temp" independently
    (`bump_outside_repo_write.py` via `tempfile.gettempdir()` alone in its
    always-allowed-roots list; `bump_out_of_repo_tool_write.py` via a
    same-shaped but separately-maintained `_target_is_bare_temp_scratch`).
    Two classifiers derived the same way are not corroboration -- collapsed
    here so both surfaces answer identically, by construction, going
    forward.

    Fails OPEN (returns `True` -- i.e. "treat as scratch, do not bump") on
    any resolution failure, matching this module's overall posture (see
    module docstring's opening paragraph): an unresolvable path gives the
    guard no way to tell what it is, and uncertainty here always resolves
    toward NOT bumping.

    `path` is walked to its NEAREST EXISTING ANCESTOR
    (`_write_bump_sink_shapes.nearest_existing_ancestor`, the SAME latent-bug
    fix C4/C5 already share) before `resolve_gitdir` is consulted --
    `resolve_gitdir` shells out with the candidate as `cwd`, which requires
    an existing DIRECTORY, and a write-sink target (or a not-yet-created
    `Write`-tool `file_path`) is very often a file/directory that does not
    exist yet. Probing the raw leaf directly would silently mis-resolve to
    "no git repo" for every not-yet-created path inside a real repo under
    the temp root -- exactly the conjunction this predicate exists to get
    right.
    """
    try:
        target_cf = _resolve_path(path)
        if target_cf is None:
            return True
        under_temp = any(
            _is_under(target_cf, root_cf) for root_cf in _all_temp_roots(env=env)
        )
        if not under_temp:
            return False
        probe_dir = nearest_existing_ancestor(path)
        if probe_dir is None:
            return True
        return resolve_gitdir(probe_dir) is None
    except Exception:
        return True


def is_agent_memory_store_path(path: str) -> bool:
    """True iff `path` resolves under `<home>/.claude/projects/<slug>/memory/`
    for ANY project slug -- Claude Code's own persistent per-project agent
    memory store, not a sibling repo, not a doctrine surface, and not a
    cross-repo delivery. Callers (C4/C5/C7's per-candidate loops) treat
    `True` as "never bump" -- the identical shape this module's sibling
    exemptions (`target_is_bare_temp_scratch`, and each surface's own
    lessons-outbox/settings-home carve-outs) already use.

    Defect this closes: nudging an agent toward `cross-repo-memo` for a
    write into its OWN harness-owned memory store is incoherent advice --
    there is no sibling repo on the other end, the path is namespaced to
    THIS project's own slug, and `guard_memory_store_cap.py` already governs
    this exact surface on its own terms (a size cap), so it should not also
    be swept up by the generic foreign-repo/outside-repo bump.

    REUSES `guard_memory_store_cap._guarded_project_roots()` -- the SAME
    `<home>/.claude/projects` root resolver that guard already carries
    (USERPROFILE/HOME/`Path.home()` union, plus `CLAUDE_HOME` treated as a
    direct `.claude` root) -- rather than a second, independently-derived
    definition of the same path shape. Deliberately NOT
    `guard_memory_store_cap._memory_target`, which additionally requires the
    candidate be a DIRECT child of `memory/` (exactly three path parts) --
    that guard's own scope is narrower (only the index file and its sibling
    body files), whereas this predicate must match the `memory/` directory
    prefix itself and anything beneath it, per this fix's own scope.

    Kept narrow ON PURPOSE: matches ONLY beneath `<slug>/memory/`, never the
    bare `<slug>/` project directory itself -- `settings.json`, `CLAUDE.md`,
    `skills/`, `plugins/`, `rules/`, `agents/`, and any other path under
    `<slug>/` that is not `memory/` are discovery/doctrine surfaces and MUST
    still bump.

    Both operands resolved via `_resolve_path` (realpath + case-fold, with
    the Windows extended-length-prefix strip already folded in) before the
    prefix comparison -- Windows first-class, matching this module's own
    "PATH COMPARISON" convention. Fails open (`False`, i.e. "not the memory
    store, evaluate the ordinary bump") on any resolution failure, matching
    this module's overall fail-toward-not-classified-away posture for a
    withheld carve-out (see `target_is_publish_destination`'s own docstring,
    "Fail-closed ON THE EXEMPTION ONLY" -- an unresolvable path here means
    the caller falls through to its ordinary verdict, never a raise).
    """
    from coordinator_core.write_guards.guard_memory_store_cap import (
        _MEMORY_DIRNAME,
        _guarded_project_roots,
    )

    target_cf = _resolve_path(path) if path else None
    if target_cf is None:
        return False
    for raw_root in _guarded_project_roots():
        root_cf = _resolve_path(str(raw_root))
        if root_cf is None:
            continue
        if not _is_under(target_cf, root_cf):
            continue
        suffix = target_cf[len(root_cf.rstrip("/")):].lstrip("/")
        parts = suffix.split("/") if suffix else []
        # `parts[0]` is the project slug, `parts[1]` must be the `memory`
        # dir itself -- at least two parts required (a bare `<slug>/memory`
        # directory write, with nothing beneath it, still counts), matching
        # `guard_memory_store_cap`'s own dir-name comparison convention
        # (case-insensitive via the already-casefolded `target_cf`/`suffix`).
        if len(parts) >= 2 and parts[1] == _MEMORY_DIRNAME.casefold():
            return True
    return False


def resolve_launch_anchor(
    session_id: str, cwd: Optional[str] = None, env: Optional[dict] = None
) -> Optional[str]:
    """The session's anchored launch directory -- PRIMARY: C0's session-start
    record (itself settings-home-first, falling back to the in-repo hub --
    see `_write_bump_session_start.read_session_start_record`); FALLBACK:
    `CLAUDE_PROJECT_DIR`, currently known-absent from a live hook subprocess
    (see module docstring, "TWO ANCHORS"). Returns `None` (fail open) when
    neither resolves -- no anchor means no way to tell where this session
    launched, so every downstream question in this module answers "does not
    apply" rather than guessing.

    `env` is threaded into `read_session_start_record` (optional, defaulting
    to `None` -- resolve from `os.environ`) so its settings-home-first read
    resolves from the SAME injected `env` mapping this function's own
    `CLAUDE_PROJECT_DIR` fallback and `_anchor_is_under_claude_home` use --
    otherwise a single `bump_applies` verdict could resolve the anchor from
    live `os.environ` while resolving the `~/.claude` fleet-recovery hatch
    from an injected `env`, inside the same decision.
    """
    if not session_id:
        return None
    primary = read_session_start_record(session_id, cwd=cwd, env=env)
    if primary:
        return primary
    env = os.environ if env is None else env
    fallback = env.get("CLAUDE_PROJECT_DIR")
    return fallback or None


def _anchor_is_under_claude_home(anchor: str, env: Optional[dict] = None) -> bool:
    """True iff `anchor` sits under `~/.claude` (the fleet-recovery hatch).

    Fail-open direction here matches the module's overall posture: if the
    home directory itself cannot be resolved (`_home_from_env` returns an
    empty string -- no `CLAUDE_HOME`/`HOME`/`USERPROFILE` in `env`), this
    returns `True` -- i.e. treats the unresolvable case as "assume the
    hatch", because the module's fail-open direction throughout is "do not
    bump", and suppressing the bump is what returning `True` here causes
    the caller to do. An unresolvable anchor path (broken symlink, race)
    also returns `True` for the identical reason.
    """
    env = os.environ if env is None else env
    home = _home_from_env(env)
    if not home:
        return True
    claude_home_cf = _resolve_path(os.path.join(home, ".claude"))
    anchor_cf = _resolve_path(anchor)
    if claude_home_cf is None or anchor_cf is None:
        return True
    return _is_under(anchor_cf, claude_home_cf)


def target_is_under_claude_home(path: str, env: Optional[dict] = None) -> bool:
    """True iff `path` resolves under `~/.claude` -- the TARGET-side
    counterpart of `_anchor_is_under_claude_home`, added by
    docs/plans/2026-08-10-carve-claude-out-and-close-the-backslash-bypass.md
    chunk C1 (AC1-AC4).

    Model is `_anchor_is_under_claude_home`, NOT `write_guards.
    bump_out_of_repo_tool_write._target_is_under_settings_home`. The
    settings-home predicate short-circuits `return False` whenever the
    caller has already resolved a `target_gitdir` (a real checkout under
    that root is still treated as a foreign repo) -- but `~/.claude` IS a
    real git checkout on this machine (`git rev-parse --show-toplevel`
    resolves inside it, verified live by the reviewer), so a predicate
    built on that same short-circuit would never fire for it, which is
    exactly why the settings-home exemption never covered `~/.claude` in
    the first place. This predicate is therefore UNCONDITIONAL on git-dir
    state -- callers consult it before, or independently of, any
    `target_gitdir is not None` check, mirroring how `is_agent_memory_
    store_path` (immediately above) already fires for a target inside
    `~/.claude` despite that same git-checkout fact.

    `~/.claude` is resolved from `env` via `trusted_root_guard.
    _home_from_env` (`CLAUDE_HOME` -> `HOME` -> `USERPROFILE`), never a
    hardcoded path -- same resolver `_anchor_is_under_claude_home` uses,
    per the plan's Anti-scope ("Do not carve out by matching on a path
    string"). `env` threads through explicitly so a single verdict cannot
    resolve the anchor from live `os.environ` while resolving THIS
    predicate from an injected mapping -- the same hazard `resolve_launch_
    anchor`'s own docstring names for the anchor side.

    Fails CLOSED on the exemption itself (`False` -- not under `~/.claude`,
    so the carve-out is withheld and the ordinary bump logic decides) when
    the home directory or `path` cannot be resolved. This does not invert
    the module's overall fail-open posture: withholding an unresolvable
    carve-out and falling through to the caller's own (already fail-open)
    verdict is the same shape `target_is_publish_destination` documents
    under "Fail-closed ON THE EXEMPTION ONLY".
    """
    env = os.environ if env is None else env
    home = _home_from_env(env)
    if not home:
        return False
    claude_home_cf = _resolve_path(os.path.join(home, ".claude"))
    target_cf = _resolve_path(path) if path else None
    if claude_home_cf is None or target_cf is None:
        return False
    return _is_under(target_cf, claude_home_cf)


def anchor_subtree_contains(anchor: str, target: str) -> bool:
    """True iff `target` resolves at or under the session's own anchored
    launch-directory SUBTREE -- the Narrow shape for a rootless session
    (PM ruling 2026-08-10, `state/bug-backlog/2026-08-10-a-session-
    anchored-outside-any-git-repo-88ca86c1f8bf.yaml`; costed recommendation
    `state/subagent-share/fe113177-19f4-4220-a8be-e2ae0c711dea/
    coordinatoreng-director-de26add3.md`).

    Consulted ONLY by the three write-confinement bump surfaces' own
    NO-REPO-ANCHOR branch (`session_anchor_has_git_repo` / `own_gitdir is
    None`) -- when the session's own anchor sits in NO git repo, today's
    unconditional allow for every UNREGISTERED cross-repo target (§ measured
    in `docs/research/spike-verdicts/2026-08-10-rootless-session-write-
    boundary.md`) narrows to "allow only inside the anchor's own subtree" --
    a REGISTERED target still bumps unconditionally, as it always has
    (`target_is_registered_repo`, unaffected by this predicate).

    NOT consulted by `bump_outside_repo_write.py`'s own no-repo-anchor bail
    (`session_anchor_has_git_repo(...) is False -> return None`, unchanged
    by this fix): that guard's own defining predicate requires the TARGET
    also resolve to no git root, so under a rootless anchor there is no
    gitdir anywhere -- neither the anchor's nor the target's -- to site a
    clearable marker at. Narrowing that specific cell would produce exactly
    the unclearable deny this shape was chosen to avoid (see the eng-
    director recommendation, "The only unclearable residue is 'outside the
    anchor subtree AND in no repo' ... can legitimately keep failing open").
    A firing denial under THIS predicate is always sited at the TARGET's own
    resolved gitdir -- a real repo, confirmed non-`None` by the caller
    before this predicate is ever consulted -- so the existing target-gitdir
    marker fallback keeps working unchanged; see this module's docstring,
    "WHAT THIS MODULE ANSWERS", point 2, for the shared no-bump condition
    this predicate narrows.

    Both operands go through `_resolve_path` (realpath + case-fold, Windows
    extended-length-prefix strip already folded in) before the SAME
    boundary-anchored `_is_under` prefix test every other predicate in this
    module uses -- `_is_under` appends a trailing `/` to the parent before
    the `startswith` check, so an anchor named `work` does NOT admit a
    sibling directory `workspace` merely sharing the leading characters of
    its own name (illustrative shape, not a machine-specific path).

    Fails OPEN (`True` -- "at/under the anchor, do not bump") on any
    resolution failure for EITHER operand, matching this module's overall
    fail-toward-not-bumping posture (module docstring's opening paragraph):
    an unresolvable anchor or target gives this predicate no way to tell
    whether containment holds, and uncertainty here always resolves toward
    NOT bumping.
    """
    anchor_cf = _resolve_path(anchor) if anchor else None
    target_cf = _resolve_path(target) if target else None
    if anchor_cf is None or target_cf is None:
        return True
    return _is_under(target_cf, anchor_cf)


def bump_applies(
    session_id: str, cwd: Optional[str] = None, env: Optional[dict] = None
) -> bool:
    """Does the write-confinement bump apply at all to this session?

    `False` (no bump, fail open) when:
      - the anchor cannot be resolved at all (`resolve_launch_anchor`
        returns `None` -- no session-start record AND no live
        `CLAUDE_PROJECT_DIR`), or
      - the anchor sits under `~/.claude` -- the fleet-recovery hatch (see
        module docstring).

    `True` otherwise. This is deliberately NOT the full verdict any
    individual guard needs -- a caller must still check
    `session_anchor_has_git_repo` / `target_is_registered_repo` for the
    second no-bump condition (see module docstring, "WHAT THIS MODULE
    ANSWERS").
    """
    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return False
    if _anchor_is_under_claude_home(anchor, env=env):
        return False
    return True


def session_anchor_has_git_repo(
    session_id: str, cwd: Optional[str] = None, env: Optional[dict] = None
) -> bool:
    """True iff the session's anchored launch directory is itself inside a
    git repo. False (fail open, in the sense of "treat as the no-repo
    case") when the anchor is unresolvable OR `git rev-parse --git-dir`
    fails against it (`_write_bump_marker.resolve_gitdir`, itself already
    fail-open, supplies the git-dir resolution -- not reimplemented here).
    Callers use this for the second no-bump condition: when this returns
    `False`, outside-repo writes never bump, and cross-repo writes bump
    only against a `target_is_registered_repo` target.
    """
    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return False
    return resolve_gitdir(anchor) is not None


def _all_registered_repo_roots(env: Optional[dict] = None) -> list:
    """Every value under a `repos.*` key across the machine-local registry
    (`registry.local.toml` precedence over `registry.toml`, both flattened
    via `machine_resolver._load_toml`/`_flatten` -- no second TOML parser).

    Fail open: an unreadable/missing registry directory or file yields an
    empty list (`_load_toml` already degrades to `{}` on any read/parse
    failure), never raises.

    `env` is accepted for call-site symmetry with this module's other
    functions but is not consulted directly -- registry LOCATION is
    resolved the same way every other reader in this package resolves it
    (`machine_resolver.registry_dir()`, which honours the real
    `MACHINE_LOCAL_REGISTRY_DIR` process env var for test isolation, same
    as `registry_get`).
    """
    reg_dir = registry_dir()
    merged: dict = {}
    for fname in ("registry.toml", "registry.local.toml"):
        flat = _flatten(_load_toml(Path(reg_dir) / fname))
        merged.update(flat)
    roots = []
    for key, value in merged.items():
        if not key.startswith(_REGISTRY_REPOS_PREFIX):
            continue
        if isinstance(value, list):
            value = "\n".join(str(i) for i in value)
        s = str(value) if value is not None else ""
        if s:
            roots.append(s)
    return roots


def target_is_registered_repo(target_root: str, env: Optional[dict] = None) -> bool:
    """Is `target_root` inside (or equal to) any `repos.*` entry in the
    machine-local registry?

    Used only for the second no-bump condition -- when the session's own
    anchor is in no git repo (`session_anchor_has_git_repo` is `False`),
    per § "Where the bump does not fire": a fresh, unregistered scaffold
    tree writes freely, but a commit into a registry-known repo still
    bumps.

    Fail open: `False` (not registered, so the caller does not bump) when
    `target_root` is empty/unresolvable, or when the registry cannot be
    read at all (`_all_registered_repo_roots` already degrades to `[]` in
    that case) -- per the plan's own § text, "If the registry is
    unreadable, nothing bumps."
    """
    target_cf = _resolve_path(target_root) if target_root else None
    if target_cf is None:
        return False
    for raw_root in _all_registered_repo_roots(env=env):
        root_cf = _resolve_path(raw_root)
        if root_cf is None:
            continue
        if _is_under(target_cf, root_cf):
            return True
    return False


def _all_publish_destinations(env: Optional[dict] = None) -> list:
    """Every `(path, owner)` pair declared under a `publish.mirrors.<name>.*`
    entry across the merged machine-local registry (`registry.toml` then
    `registry.local.toml`, later wins -- identical merge order to
    `_all_registered_repo_roots`, no second TOML parser).

    C1 (docs/plans/2026-08-03-narrow-write-confinement-bump.md).

    DRIFT D4 -- the two mirrors' `.owner` keys live in DIFFERENT files at
    HEAD (`publish.mirrors.coordinator_claude.owner` in `registry.toml`,
    `publish.mirrors.claude_klabauter.owner` and BOTH `.path` values in
    `registry.local.toml`). Reading only `registry.local.toml` would
    silently lose `coordinator_claude`'s owner. The merge above reads both
    files, exactly as `_all_registered_repo_roots` already does.

    DRIFT D5 -- an entry whose `.path` value is empty (declared but not
    yet overridden -- the same shape `registry.toml`'s `repos.*` keys use
    at HEAD) is skipped entirely; it must never resolve to the filesystem
    root. `.owner` may still be declared for a name whose `.path` is empty
    -- such a name simply does not appear in the returned list, since
    there is no root to compare against yet.

    Fail open: an unreadable/missing registry directory or file yields an
    empty list (`_load_toml` already degrades to `{}`), never raises.

    `env` is accepted for call-site symmetry with this module's other
    functions but is not consulted directly -- registry LOCATION is
    resolved the same way every other reader in this package resolves it
    (`machine_resolver.registry_dir()`, which honours the real
    `MACHINE_LOCAL_REGISTRY_DIR` process env var for test isolation, same
    as `registry_get`).
    """
    reg_dir = registry_dir()
    merged: dict = {}
    for fname in ("registry.toml", "registry.local.toml"):
        flat = _flatten(_load_toml(Path(reg_dir) / fname))
        merged.update(flat)
    names = set()
    for key in merged:
        if key.startswith(_REGISTRY_PUBLISH_MIRRORS_PREFIX) and key.endswith(
            _PUBLISH_PATH_SUFFIX
        ):
            name = key[len(_REGISTRY_PUBLISH_MIRRORS_PREFIX) : -len(_PUBLISH_PATH_SUFFIX)]
            names.add(name)
    destinations = []
    for name in sorted(names):
        path_key = f"{_REGISTRY_PUBLISH_MIRRORS_PREFIX}{name}{_PUBLISH_PATH_SUFFIX}"
        owner_key = f"{_REGISTRY_PUBLISH_MIRRORS_PREFIX}{name}{_PUBLISH_OWNER_SUFFIX}"
        raw_path = merged.get(path_key)
        if isinstance(raw_path, list):
            raw_path = "\n".join(str(i) for i in raw_path)
        path_s = str(raw_path) if raw_path is not None else ""
        if not path_s:
            continue
        raw_owner = merged.get(owner_key)
        owner_s = str(raw_owner) if raw_owner is not None else ""
        destinations.append((path_s, owner_s))
    return destinations


def _all_publish_destination_roots(env: Optional[dict] = None) -> list:
    """Every `publish.mirrors.*.path` value across the merged registry --
    roots-only view of `_all_publish_destinations`, mirroring
    `_all_registered_repo_roots`'s own shape for callers that need only the
    path set. `target_is_publish_destination` is that caller (see its own
    docstring).

    `env` is accepted for call-site symmetry, unused for the same reason
    `_all_publish_destinations`/`_all_registered_repo_roots` leave it
    unused -- see those docstrings.
    """
    return [path for path, _owner in _all_publish_destinations(env=env)]


def target_is_publish_destination(target_root: str, env: Optional[dict] = None) -> bool:
    """Is `target_root` inside (or equal to) any `publish.mirrors.*.path`
    entry in the machine-local registry?

    Closed set, never a path-pattern match (plan § Anti-scope: "Do not
    match publish destinations by path pattern"). Membership alone decides
    the verdict -- `owner` is consulted only by `publish_destination_owner`,
    never here (plan § Anti-scope: "Do not gate the verdict on `owner`").

    `repos.*` and `publish.mirrors.*` are disjoint namespaces (verified at
    HEAD -- plan § Design), so a sibling source repo registered under
    `repos.*` can never match this test.

    Fail-closed ON THE EXEMPTION ONLY: `False` (not a publish destination,
    so the carve-out is withheld and the ordinary foreign-repo bump fires)
    when `target_root` is empty/unresolvable, or when the registry cannot
    be read at all (`_all_publish_destinations` already degrades to `[]`
    in that case). This does not invert the module's overall fail-open
    posture -- see module docstring's opening paragraph and plan § Design:
    withholding a carve-out is the module's existing fail-open direction
    expressed one level down, not a new deny-on-uncertainty branch.

    Consumes `_all_publish_destination_roots` as its roots source, mirroring
    how `target_is_registered_repo` consumes `_all_registered_repo_roots` --
    same shape, same reason: the membership test only needs the path set,
    never the owner. Review: code-reviewer a19c32b7, Finding 1.
    """
    target_cf = _resolve_path(target_root) if target_root else None
    if target_cf is None:
        return False
    for raw_root in _all_publish_destination_roots(env=env):
        root_cf = _resolve_path(raw_root)
        if root_cf is None:
            continue
        if _is_under(target_cf, root_cf):
            return True
    return False


def publish_destination_owner(target_root: str, env: Optional[dict] = None) -> str:
    """The registered `owner` of the `publish.mirrors.*` entry `target_root`
    falls under, or `""` when `target_root` is not a publish destination
    (mirrors `target_is_publish_destination`'s own membership test) or the
    registry cannot be read. Consumed by C2's copy for context only -- see
    `target_is_publish_destination`'s docstring: `owner` never gates the
    verdict, only informs the message.
    """
    target_cf = _resolve_path(target_root) if target_root else None
    if target_cf is None:
        return ""
    for raw_root, owner in _all_publish_destinations(env=env):
        root_cf = _resolve_path(raw_root)
        if root_cf is None:
            continue
        if _is_under(target_cf, root_cf):
            return owner
    return ""


def record_applicability_event(
    session_id: str,
    repo: str,
    target: str,
    agent_class: str,
    cwd: Optional[str] = None,
) -> None:
    """Append ONE line to a per-session, append-only observability log
    (AC18) -- `repo`, `target`, `session_id`, `agent_class`, timestamped.

    Best-effort ONLY: this exists so the PM's "prevent 95% of missteps"
    claim is checkable in a month rather than a shrug (see module
    docstring, "OBSERVABILITY"). A failed append -- unwritable disk, no
    session hub resolvable, a permission error, a race -- is silently
    swallowed and NEVER blocks or alters the write the caller is
    otherwise permitting/denying. Never raises.

    Callers (C4/C5/C7) are expected to call this exactly when their own
    verdict is "the bump fires" -- this module does not call it itself
    from `bump_applies`, because `bump_applies` answers only the
    APPLICABILITY question, not the final per-guard verdict (a session for
    which the bump "applies" may still not bump on a same-repo write, a
    read, or a carved-out CLI invocation -- logging at the applicability
    layer would over-record relative to what actually bumped).
    """
    if not session_id:
        return
    try:
        sdir = session_dir(session_id, cwd)
    except ValueError:
        return
    if not sdir:
        return
    try:
        Path(sdir).mkdir(parents=True, exist_ok=True)
        line = (
            f"{now_iso()} repo={repo} target={target} "
            f"session={session_id} agent_class={agent_class}\n"
        )
        with open(
            Path(sdir) / _APPLICABILITY_LOG_FILENAME, "a", encoding="utf-8", newline="\n"
        ) as f:
            f.write(line)
    except Exception:
        # Fail open, unconditionally -- see module docstring. A missed log
        # line is never a reason to alter or block the write it describes.
        pass
