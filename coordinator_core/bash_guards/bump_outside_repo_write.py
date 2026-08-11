"""coordinator_core.bash_guards.bump_outside_repo_write -- the Bash-surface
OUTSIDE-REPO write-confinement speed bump (C5): a well-meaning session writes
somewhere that resolves under NO git root at all -- a throwaway scratch
directory it invented on the spot -- and nothing today notices. The two real
observed incidents this plan cites as its evidence base (see the plan's own
§ Problem for the two literal paths, quoted there under the identical
`abs-path-ok` marker -- not repeated here to avoid a second per-machine
literal in this codebase) are exactly this shape.

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md [example-doctrine-repo
repo], chunk C5 "Outside-repo detection, inline-interpreter classification,
sandbox reroute".

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY. Read the plan's "Design
posture -- passable by construction" section before touching this module.
FAIL OPEN, EVERYWHERE: every unresolvable step (no anchor, no gitdir cannot
be probed, an unparseable command) returns `None` (no bump) -- the OPPOSITE
of this package's fail-closed neighbours. A bump that misfires on legitimate
work gets disabled, and a disabled guard prevents nothing.

PREDICATE -- "RESOLVES UNDER NO GIT ROOT". This is the one thing that
distinguishes this guard from its sibling `bump_foreign_repo_write.py` (C4):
C4 fires when a write TARGET resolves under a DIFFERENT git root than the
session's own; this guard fires when a write TARGET resolves under NO git
root at all. A candidate whose `resolve_gitdir` DOES resolve to something is
never this guard's concern (same repo, a different repo, or a registered
repo -- all C4's territory or simply allowed) -- see `_iter_write_sink_
candidates` below, which SKIPS any candidate with a resolved gitdir.

DESTINATION CLASS AND MARKER, PER C3's SCOPE CORRECTION
(docs/plans/2026-08-03-narrow-write-confinement-bump.md): this guard's
OUTSIDE_ANY_REPO class keeps today's marker at the SESSION's own anchor
gitdir, UNCHANGED -- this guard's own defining predicate above guarantees no
target gitdir exists to relocate a marker into, so C3's per-target
narrowing is a structural no-op for this class. It DOES consume C1's
`target_is_publish_destination` classifier for the message's
`destination_class` axis (always resolving `DESTINATION_FOREIGN` in
practice, since a `publish.mirrors.*.path` entry is itself always a real
repo, and this guard's predicate has already excluded any target that
resolves to one) -- classified explicitly rather than hardcoded, so this
guard never silently drifts from C1's own closed set.

WHERE THIS GUARD DOES NOT FIRE AT ALL (§ "Where the bump does not fire",
second bullet) -- when the SESSION's OWN anchored launch root is ITSELF in
no git repo, outside-repo writes never bump, full stop, regardless of the
target: a session launched in `Documents` to scaffold a brand-new project
writes freely into its own new tree. `_write_bump_applicability.
session_anchor_has_git_repo` answers this and is checked FIRST, before any
candidate extraction -- see `check_bump_outside_repo_write`'s own docstring.
(Cross-repo writes in that same no-repo-anchor shape still bump against a
REGISTERED target -- that is C4's `target_is_registered_repo` branch, not
this guard's; this guard's job is the "does not bump" half of that same
plan sentence.)

NARROW (PM ruling 2026-08-10, `state/bug-backlog/2026-08-10-a-session-
anchored-outside-any-git-repo-88ca86c1f8bf.yaml`) DOES NOT TOUCH THIS
GUARD. Narrow confines a rootless session's write boundary to its own
anchor-directory subtree (`_write_bump_applicability.
anchor_subtree_contains`, wired into C4/C7's own no-repo-anchor branches),
but this guard's defining predicate REQUIRES the target ALSO resolve to no
git root at all -- under a rootless anchor that means neither the anchor
nor the target has a gitdir anywhere, so there is no clearable marker site
to narrow toward (see `anchor_subtree_contains`'s own docstring, and the
eng-director recommendation this ruling shipped: "outside the anchor
subtree AND in no repo ... can legitimately keep failing open"). The
no-repo-anchor bail two paragraphs above is therefore UNCHANGED by Narrow.

Reuses rather than reimplements (mirrors C4's own reuse list exactly, since
these are the SAME shared modules C4 already consumes):
  - `_command_tokenizer.resolve_command_positions` -- the resolve-once entry
    point. ALSO already auto-recurses `bash`/`sh`/`zsh -c '<payload>'`
    (`_SHELL_INTERPRETERS_WITH_C`) into additional entries in the SAME
    returned list, at `depth > 0` -- this module iterates every depth, not
    only depth 0 (contrast C4, which deliberately stays at depth 0 because
    `-c`-payload classification was explicitly this chunk's scope, not
    C4's), so those three interpreters' inline payloads are ALREADY covered
    for free by the shared tokenizer, no extra code needed here.
  - `_write_bump_applicability` (C2) -- whether the bump applies at all, and
    (this guard's own extra check) whether the session's own anchor has a
    git repo at all.
  - `_write_bump_marker` (C3) -- the session-scoped clear-once marker.
  - `_write_bump_message` (C6) -- ALL user-facing copy.
  - `_write_bump_sink_shapes` -- the SAME plain-bash write-sink shape table
    C4 owns and this module imports BY REFERENCE, never restates (module's
    own docstring: "so the two chunks classify the identical shape set
    rather than each hand-rolling its own list that can silently drift").
    Also supplies `nearest_existing_ancestor` (lifted from C4 during this
    chunk's own landing so both guards share one copy -- see that
    function's docstring for the latent-bug history).
  - `block_subagent_destructive_action._BUNDLED_C_FLAG_RE` /
    `._C_FLAG_INTERPRETERS` (imported here via the same path
    `_sentinel_creation_guard.py` itself re-exports them through, per this
    chunk's own reuse instruction) -- see "INLINE PYTHON `-c` PAYLOADS"
    below for why THIS module still needs its own extraction, unlike the
    three shell interpreters the shared tokenizer already recurses.

INLINE PYTHON `-c` PAYLOADS ARE CLASSIFIED, FILE-INVOKED SCRIPTS STAY EXEMPT
(PM ruling). `_command_tokenizer._SHELL_INTERPRETERS_WITH_C` covers only
`sh`/`bash`/`zsh`/`dash`/`ksh` -- NOT `python`/`python3` -- so a `python3 -c
'<payload>'` invocation is NOT auto-recursed by `resolve_command_positions`
the way a `bash -c` one already is. This module adds exactly that one
missing leg: for each depth-0 segment whose (version-normalized) head is a
member of `_C_FLAG_INTERPRETERS`, `_extract_inline_c_payload` (mirroring
`_command_tokenizer._extract_dash_c_payload`'s own bundled/standalone `-c`
scan, reusing `_BUNDLED_C_FLAG_RE` rather than re-deriving the pattern)
pulls the payload string and re-runs it through `resolve_command_positions`
+ write-sink extraction, exactly as if it were its own top-level command --
the same best-effort, string-shaped classification `_sentinel_creation_
guard._evaluate_segment_indirection` already applies for its own sentinel-
protection purpose, reused here for the identical reason: an agent that
embeds a shell-shaped write (`"cp x /elsewhere/y"`, `"echo x >
/elsewhere/y"`) as a Python `-c` string argument is a real, observed misstep
shape, not an adversarial one, and this module does not attempt to parse
actual Python semantics -- it is a speed bump, not an interpreter.
Deliberately applied to depth-0 segments only, with `bash`/`sh`/`zsh` heads
harmlessly re-matching too (their own inline `-c` payload is ALREADY yielded
by the tokenizer's own recursion above; running this extraction again on the
same payload text just re-derives the identical candidate a second time --
fail-open direction, never a false miss, never a false deny, so not worth
excluding by name).

FILE-INVOKED interpreter scripts (`python3 install.py`, `bash install.sh`)
are NOT classified here, on purpose -- deferred to plan D1, not this
chunk's scope (PM ruling, restated because it is this plan's single most
easily "corrected" decision): those are authored, reviewed, versioned
artifacts, and `python3 install.py`/`bash install.sh` is the very install
path this plan's own C0/C8 chunks protect. Classifying them would mean
reading file CONTENTS at guard time -- a real perf cost and a real
false-positive risk against exactly that path. See the test suite's
explicit negative case.

WRITE-SINK CLASSIFICATION -- the enumerated set (AC20) is owned by
`_write_bump_sink_shapes.WRITE_SINK_BINARIES` / `extract_write_sink_
targets_for_segment`, imported here BY REFERENCE, never restated: output
redirection (`>`, `>>`), `tee`, `cp`, `mv`, `mkdir -p`, `install`, `sed -i`,
heredocs (`cat > ... <<EOF`), `rsync`, `tar -x -C`. Deliberately NOT covered,
same reason C4 states: any shape requiring adversarial-style interpreter
indirection beyond the single inline `-c` case above -- out of scope per §
Design posture's "do not enumerate evasions" rule, not an oversight. This
module does NOT handle `git -C <dir> <write>` / `cd <dir> && git <write>`
shapes at all (contrast C4, which does) -- AC3 names "a plain-bash write
resolving to no git repo" specifically, and a `git <write-subcommand>`
invoked against a location with no git repo at all is not a write a real
`git` binary would ever perform (it exits `fatal: not a git repository`
before touching anything) -- there is no write to bump in that shape, so
adding git-subcommand handling here would be dead code, not missing
coverage.

ALWAYS-ALLOWED DESTINATIONS (AC9) -- settings home, session scratchpad,
subagent sandbox root, system temp, resolved through EXISTING primitives,
never literals:
  - settings home: `trusted_root_guard._settings_home_dir_from_env` (same
    helper C4 already reuses for the same reason).
  - system temp AND session scratchpad, together --
    `_write_bump_applicability.target_is_bare_temp_scratch`, consulted
    directly in `_target_is_always_allowed` rather than through this
    module's own root-containment list. CORRECTED CLAIM (this section
    previously asserted the scratchpad lives "under system temp" in the
    sense `tempfile.gettempdir()` alone would catch -- confirmed FALSE, live
    on macOS: `TMPDIR`/`gettempdir()` resolves under `/var/folders/...`,
    while the harness-designated per-session scratchpad lives under
    `/private/tmp`, a path `gettempdir()` never reports and never contains).
    Before the fix, this guard bumped on every scratchpad write -- memo
    drafts, scratch scripts, intermediate files -- exactly the false
    positive the shared classifier's own docstring documents. The shared
    classifier's "recognized temp roots" set additionally resolves
    `os.path.realpath("/tmp")` (what actually catches the scratchpad on
    macOS, via the `/tmp` -> `/private/tmp` symlink) and the `TMPDIR`/
    `TEMP`/`TMP` environment values directly -- see that function's own
    docstring for the full set and the fail-open/conjunctive-with-no-git-repo
    contract this module now depends on rather than re-deriving.
  - subagent sandbox root: `state/subagent-share/<effective-session-id>/`
    under the resolved git root, the SAME convention `bump_foreign_repo_
    write._sandbox_root_hint` already uses (mirrored here rather than
    imported, since that function is private to a sibling module this
    chunk does not otherwise touch -- see `_sandbox_root` below). In the
    ordinary case this path is itself INSIDE the session's own repo, so it
    never even reaches this guard's "no git root" predicate at all; it is
    still named and carved out explicitly (rather than left as an
    accidental non-match) so AC9 is checkable as a real property of this
    module's own logic, not an artifact of the sandbox happening to live
    inside a repo on this fleet today.

SUBAGENTS GET THE SANDBOX NAMED AS THE SANCTIONED ROUTE -- OFFERED, NOT
ENFORCED (the Director of Engineering finding 4, restated from C4's own docstring because it is
equally load-bearing here): `write_guards/engine.py:32-37` records that this
fleet's PRIOR tool-surface out-of-repo write confinement
(`coordinator_core.subagent_sandbox`) was removed on 2026-07-15 because it
hard-denied legitimate writes outside a narrow sandbox and surprised EMs and
subagents. That confinement is NO LONGER enforced on the write path -- naming
the sandbox as a route in the subagent-class message implies availability,
not a guarantee. The EM-class message instead gets the marker line, exactly
as C4's does.

REGISTRATION (`dispatch.py`'s `_build_guard_chain`) -- mirrors C4 exactly,
every attribute named explicitly: `fail_closed=False` (the OPPOSITE of every
neighbouring `CONFINEMENT_DENY` entry -- a crash here must swallow to
"allow", never route through the hard-deny crash path, or a deliberately
passable bump becomes an accidental hard wall the moment this module has a
bug), `band=GuardBand.ADVISORY_REWRITE` (NOT `CONFINEMENT_DENY` -- the
blanket-disarm marker can suppress every band except `CONFINEMENT_DENY`, so
registering a deliberately passable bump there would make it the single
LEAST passable, least disarmable guard in the suite, matching C7's
`CLASS = 'advisory'` choice on the tool-surface guard for cross-surface
consistency), explicit `advisory_value` (never the `UNCLASSIFIED` default).

Negative-spec:
  - Does NOT add fail-closed behaviour anywhere -- see § Design posture.
  - Does NOT add unforgeability machinery to the marker -- consumes C3's
    marker exactly as written.
  - Does NOT enumerate evasions -- no adversarial interpreter-indirection
    beyond the single inline `-c` case above, no brace-expansion handling,
    no `git`-subcommand handling (see "WRITE-SINK CLASSIFICATION" above for
    why that specific omission is deliberate, not an oversight).
  - Does NOT classify file-invoked interpreter scripts -- deferred to D1,
    a PM-ruled deferral, not this chunk's gap.
  - Does NOT compose a gitdir path -- every gitdir this module touches
    comes from `_write_bump_marker.resolve_gitdir`.
  - Does NOT resolve applicability from the live payload `cwd` -- see C2's
    own "ANCHORING" docstring section for why.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    ResolutionConfidence,
    normalize_executable_basename,
    resolve_command_positions,
)
from coordinator_core.bash_guards._sentinel_creation_guard import (
    _BUNDLED_C_FLAG_RE,
    _C_FLAG_INTERPRETERS,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._write_bump_applicability import (
    bump_applies,
    is_agent_memory_store_path,
    publish_destination_owner,
    record_applicability_event,
    resolve_launch_anchor,
    session_anchor_has_git_repo,
    target_is_bare_temp_scratch,
    target_is_publish_destination,
    target_is_under_claude_home,
)
from coordinator_core.bash_guards._write_bump_marker import (
    bump_is_cleared,
    effective_session_id,
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
from coordinator_core.trusted_root_guard import _settings_home_dir_from_env
from coordinator_core.write_guards._case_fold_path import casefold_path


def _deny(reason: str) -> Dict[str, Any]:
    """Same envelope shape as C4's own `_deny` -- inlined for the identical
    reason that module states: no dependency on `dispatch_checks.py` for two
    lines of dict literal."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _resolve_and_casefold(raw: str) -> Optional[str]:
    """`os.path.realpath` then `casefold_path`, or `None` on any resolution
    failure. Mirrors `bump_foreign_repo_write._resolve_and_casefold` /
    `_write_bump_applicability._resolve_path` exactly so every guard in this
    wave agrees on what "resolved" means for a path comparison."""
    if not raw:
        return None
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        return None
    return casefold_path(resolved)


def _is_under(candidate_cf: str, parent_cf: str) -> bool:
    """Case-folded prefix containment -- both inputs already resolved via
    `_resolve_and_casefold`. Mirrors `_write_bump_applicability._is_under`."""
    parent_stripped = parent_cf.rstrip("/")
    return candidate_cf == parent_stripped or candidate_cf.startswith(parent_stripped + "/")


# ---------------------------------------------------------------------------
# AC9 -- always-allowed destinations. See module docstring for the full
# resolution rationale of each.
# ---------------------------------------------------------------------------


def _sandbox_root(git_root: Optional[str], session_id: str) -> str:
    """`state/subagent-share/<session_id>/` under `git_root` -- mirrors
    `bump_foreign_repo_write._sandbox_root_hint` exactly (private to that
    sibling module, restated here rather than imported). Returns `""` when
    either input is empty -- no fabricated path for an unresolvable case."""
    if not git_root or not session_id:
        return ""
    return str(Path(git_root) / "state" / "subagent-share" / session_id)


def _always_allowed_roots(
    anchor_git_root: Optional[str], effective_sid: str, env: Optional[dict] = None
) -> List[str]:
    """Every root a write under this predicate must NEVER bump against
    (AC9), resolved through existing primitives -- see module docstring,
    "ALWAYS-ALLOWED DESTINATIONS". Silently drops any root this process
    cannot resolve (fail open) rather than raising."""
    env = os.environ if env is None else env
    roots: List[str] = []

    settings_home = _settings_home_dir_from_env(env)
    if settings_home:
        roots.append(settings_home)

    # System temp / session scratchpad are NOT handled through this
    # containment-list mechanism -- see `_target_is_always_allowed`'s own
    # call to the shared `target_is_bare_temp_scratch` classifier, which
    # covers the full recognized-temp-root set (not `tempfile.gettempdir()`
    # alone -- see that classifier's docstring for why the harness
    # scratchpad needs more than `gettempdir()` on macOS).

    sandbox = _sandbox_root(anchor_git_root, effective_sid)
    if sandbox:
        roots.append(sandbox)

    return roots


def _target_is_always_allowed(
    target_dir: str, anchor_git_root: Optional[str], effective_sid: str, env: Optional[dict] = None
) -> bool:
    target_cf = _resolve_and_casefold(target_dir)
    if target_cf is None:
        return False

    # System temp / session scratchpad -- shared classifier (see module
    # docstring, "ALWAYS-ALLOWED DESTINATIONS"), NOT the settings-home/
    # sandbox containment-list mechanism below: `target_is_bare_temp_scratch`
    # both covers the full recognized-temp-root set AND re-asserts the
    # no-git-repo conjunction itself, so it is correct even if a future
    # caller ever invokes this helper without first excluding
    # git-resolved candidates the way `check_bump_outside_repo_write` does
    # today.
    if target_is_bare_temp_scratch(target_dir, env=env):
        return True

    # Agent memory store -- Claude Code's own per-project persistent memory
    # (`<home>/.claude/projects/<slug>/memory/**`), governed on its own
    # terms by `guard_memory_store_cap.py` and never a sibling repo or a
    # cross-repo delivery -- see `is_agent_memory_store_path`'s own
    # docstring for the false-positive this closes.
    if is_agent_memory_store_path(target_dir):
        return True

    # ~/.claude carve-out (docs/plans/2026-08-10-carve-claude-out-and-
    # close-the-backslash-bypass.md, C1, AC1-AC4). Unconditional, like the
    # agent-memory check immediately above: `~/.claude` IS a real git
    # checkout on this fleet, so it must not be gated on a resolved
    # `target_gitdir` the way `_target_is_under_settings_home` gates the
    # tool-surface settings-home exemption -- see `target_is_under_claude_
    # home`'s own docstring.
    if target_is_under_claude_home(target_dir, env=env):
        return True

    for raw_root in _always_allowed_roots(anchor_git_root, effective_sid, env=env):
        root_cf = _resolve_and_casefold(raw_root)
        if root_cf is None:
            continue
        if _is_under(target_cf, root_cf):
            return True
    return False


# ---------------------------------------------------------------------------
# AC4 -- inline `python`/`python3 -c` payload unwrap. bash/sh/zsh are
# already unwrapped for free by `resolve_command_positions` itself (module
# docstring, "INLINE PYTHON `-c` PAYLOADS").
# ---------------------------------------------------------------------------


def _extract_inline_c_payload(tokens_after_interpreter: List[str]) -> Optional[str]:
    """Mirrors `_command_tokenizer._extract_dash_c_payload`'s own scan
    (standalone `-c` or a bundled short flag containing `c`, e.g. `-ic`),
    reusing `_BUNDLED_C_FLAG_RE` per this chunk's own reuse instruction
    rather than re-deriving the pattern. Returns the token immediately
    following the flag, or `None` if no such flag/payload is present."""
    n = len(tokens_after_interpreter)
    for i, tok in enumerate(tokens_after_interpreter):
        is_c_flag = tok == "-c" or (
            tok != "-"
            and tok.startswith("-")
            and not tok.startswith("--")
            and _BUNDLED_C_FLAG_RE.match(tok)
        )
        if is_c_flag:
            return tokens_after_interpreter[i + 1] if i + 1 < n else None
    return None


# ---------------------------------------------------------------------------
# Candidate extraction -- plain-bash write sinks only (see module docstring,
# "WRITE-SINK CLASSIFICATION", for why this deliberately excludes git).
# ---------------------------------------------------------------------------


def _iter_write_sink_candidates(cmd: str, cwd: Optional[str]) -> Iterator[Tuple[str, str, str]]:
    """Yield `(target_dir, label, raw_target)` for every write-sink candidate
    this command carries -- every depth of `resolve_command_positions`'s own
    result (so `bash`/`sh`/`zsh -c` payloads, already unwrapped by that
    shared tokenizer, are covered for free), PLUS a manual unwrap of any
    depth-0 `python`/`python3 -c` payload (the one leg the shared tokenizer
    does not auto-recurse -- see module docstring).

    `raw_target` (R1, docs/plans/2026-08-08-the-bump-message-never-showed-
    the-operat.md) is the literal token this segment carried BEFORE
    `_resolve_relative`/`translate_msys_path` touched it -- captured here,
    at the point of extraction, rather than reconstructed later from
    `target_dir` (AC2: a reconstructed value is the translated path wearing
    a different label, not a raw token).

    `cd` cwd-tracking applies only at depth 0 -- a `cd` inside a nested `-c`
    payload does not move the OUTER command's own effective cwd, and
    tracking it there too is disproportionate for a passable speed bump
    (fail-open direction: under-tracking a nested `cd` only means an
    under-resolved candidate path, dropped harmlessly by the caller's own
    `resolve_gitdir` check, never a false deny).

    `target_dir` is NOT YET resolved to a git root -- callers do that via
    `resolve_gitdir`/`nearest_existing_ancestor`.

    `resolve_command_positions` (both here and the nested `-c`-payload
    re-entry below) is called with `preserve_windows_backslashes=
    _host_is_windows()` -- see `bump_foreign_repo_write._iter_write_sink_
    candidates`'s own docstring (C2, docs/plans/2026-08-10-carve-claude-out-
    and-close-the-backslash-bypass.md, AC5-AC6) for the mangled-token defect
    this closes; both write-bump guards share this ONE opt-in flag on the
    shared tokenizer rather than each re-deriving their own normalization.
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
        if rc.confidence == ResolutionConfidence.UNRESOLVED or not rc.tokens:
            continue
        head_base = normalize_executable_basename(rc.tokens[0])

        if rc.depth == 0 and head_base == "cd":
            positional = [t for t in rc.tokens[1:] if not t.startswith("-")]
            if len(positional) == 1:
                # `None` means untranslatable -- a `cd` we cannot resolve
                # must not poison every subsequent candidate in this
                # segment, so leave `effective_cwd` at its previous value
                # rather than clobbering it with `None`.
                resolved = _resolve_relative(effective_cwd, positional[0])
                if resolved is not None:
                    effective_cwd = resolved
            continue

        for raw_target in extract_write_sink_targets_for_segment(rc.tokens, head_base):
            resolved_target = _resolve_relative(effective_cwd, raw_target)
            if resolved_target is None:
                continue
            yield (resolved_target, head_base, raw_target)

        if rc.depth == 0 and head_base in _C_FLAG_INTERPRETERS:
            inline_payload = _extract_inline_c_payload(rc.tokens[1:])
            if not inline_payload:
                continue
            try:
                nested_segments = resolve_command_positions(
                    inline_payload, preserve_windows_backslashes=_host_is_windows()
                )
            except Exception:  # noqa: BLE001 -- fail open
                continue
            for nrc in nested_segments:
                if nrc.confidence == ResolutionConfidence.UNRESOLVED or not nrc.tokens:
                    continue
                nested_head = normalize_executable_basename(nrc.tokens[0])
                for raw_target in extract_write_sink_targets_for_segment(nrc.tokens, nested_head):
                    resolved_target = _resolve_relative(effective_cwd, raw_target)
                    if resolved_target is None:
                        continue
                    yield (resolved_target, nested_head, raw_target)


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------


def check_bump_outside_repo_write(
    cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """The Bash-surface outside-repo write-confinement bump (C5). Returns a
    deny envelope (the bump message) when this command's own write-sink
    target resolves under NO git root at all; `None` (allow, silently) in
    every other case, including every unresolvable one -- see module
    docstring, § Design posture.

    Checked in order:
      1. `bump_applies` (C2) -- the fleet-recovery hatch and anchor
         resolvability.
      2. `session_anchor_has_git_repo` -- if the SESSION's own anchor has NO
         git repo, this guard never bumps at all, regardless of target (§
         "Where the bump does not fire", second bullet; see module
         docstring).
      3. Per candidate: skip anything that DOES resolve to a git root (not
         this guard's concern), skip anything under an always-allowed root
         (AC9), then bump on the first remaining candidate, unless the
         marker (checked against the session's own anchor gitdir) already
         clears it.
    """
    # DIALECT GATE (C4e, 2026-08-07 -- guard-dialect-coverage.md row 15;
    # follow-up dispatch, same date, converted the blanket PowerShell SILENT
    # below into real detection once the PM authorized extending this
    # cohort's scope into `_write_bump_sink_shapes.py`). `Dialect.POWERSHELL`
    # now routes to `_check_bump_outside_repo_write_powershell`, which
    # detects ONLY the cmdlet-shaped write table C3's triage named as the
    # genuinely unmatched gap (`New-Item`/`Set-Content`/`Add-Content`/
    # `Copy-Item`/`Move-Item`/`Out-File`/`Tee-Object`, added to
    # `_write_bump_sink_shapes.py` as `PS_WRITE_SINK_CMDLETS`/
    # `extract_write_sink_targets_powershell`) -- `cp`/`mv` PowerShell
    # aliases are NOT duplicated here (already fire via alias collision
    # elsewhere) and `>`/`>>` are left alone (same operator characters in
    # both dialects, not this leg's to re-derive). Every other PowerShell
    # shape -- a bare redirect, an alias, an unrecognized cmdlet, or a
    # command this dialect's tokenizer cannot parse at all -- still records
    # SILENT and declines, per "prefer SILENT to a guess wherever PowerShell
    # semantics are unclear" (see that function's own docstring). `Dialect.
    # BASH`/`None` falls through unchanged below (AC4).
    dialect = dialect_from_tool_name(payload.get("tool_name") if isinstance(payload, dict) else None)
    if dialect is Dialect.POWERSHELL:
        return _check_bump_outside_repo_write_powershell(cmd, session_id, cwd, payload)

    if not cmd or not cmd.strip() or not session_id:
        return None

    env = os.environ

    if not bump_applies(session_id, cwd=cwd, env=env):
        return None

    if not session_anchor_has_git_repo(session_id, cwd=cwd, env=env):
        return None

    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return None
    anchor_gitdir = resolve_gitdir(anchor)
    if anchor_gitdir is None:
        # Contradicts `session_anchor_has_git_repo` above only under a race
        # (repo vanished between the two calls) -- fail open.
        return None

    agent_id = payload.get("agent_id") or "" if isinstance(payload, dict) else ""
    anchor_git_root_str = str(anchor_gitdir.parent) if anchor_gitdir.name == ".git" else str(anchor_gitdir)
    effective_sid = effective_session_id(session_id, anchor_git_root_str, agent_id)

    for target_dir, _label, raw_target in _iter_write_sink_candidates(cmd, cwd):
        probe_dir = _nearest_existing_ancestor(target_dir)
        if probe_dir is None:
            # No existing ancestor at all -- cannot resolve a git root
            # either way; fail open.
            continue
        target_gitdir = resolve_gitdir(probe_dir)
        if target_gitdir is not None:
            # Resolves under SOME git root -- not this guard's predicate
            # (same repo, a different repo, or a registered repo -- all C4's
            # territory or already allowed).
            continue

        if _target_is_always_allowed(probe_dir, anchor_git_root_str, effective_sid, env=env):
            continue

        if bump_is_cleared(anchor, session_id, git_root=anchor_git_root_str, agent_id=agent_id):
            continue

        agent_class = resolve_agent_class(payload if isinstance(payload, dict) else {}, anchor_git_root_str)
        sandbox_root = (
            _sandbox_root(anchor_git_root_str, effective_sid) if agent_class == AGENT_CLASS_SUBAGENT else ""
        )

        # C1 -- classify via the SAME closed-set membership test C4 uses.
        # This guard's own defining predicate (`target_gitdir is not None:
        # continue`, above) means `target_dir` never resolves to a git repo
        # by the time execution reaches here, so a `publish.mirrors.*.path`
        # match (itself always a real repo) is structurally unreachable --
        # this always classifies DESTINATION_FOREIGN in practice. Classified
        # explicitly anyway (rather than hardcoding DESTINATION_FOREIGN)
        # per C5's own instruction to consume C1 for classification, not
        # re-derive or assume it.
        destination_class = (
            DESTINATION_PUBLISH
            if target_is_publish_destination(target_dir, env=env)
            else DESTINATION_FOREIGN
        )
        destination_owner = (
            publish_destination_owner(target_dir, env=env)
            if destination_class == DESTINATION_PUBLISH
            else ""
        )

        record_applicability_event(
            session_id,
            repo=anchor_git_root_str,
            target=target_dir,
            agent_class=agent_class,
            cwd=cwd,
        )

        message = render_bump_message(
            agent_class=agent_class,
            target_repo="no git repo (%s)" % target_dir,
            session_repo=anchor_git_root_str,
            gitdir=anchor_gitdir,
            session_id=effective_sid,
            sandbox_root=sandbox_root,
            destination_class=destination_class,
            destination_owner=destination_owner,
            raw_target=raw_target if raw_target != target_dir else "",
        )
        return _deny(message)

    return None


# ---------------------------------------------------------------------------
# PowerShell-dialect leg (C4e follow-up, 2026-08-07, guard-dialect-coverage.md
# row 15). Mirrors `check_bump_outside_repo_write`'s own applicability/
# marker/message wiring exactly -- kept as a fully separate function (not
# interleaved into the bash body) so the bash leg's AC4 byte-identical-
# behaviour requirement carries zero risk from this addition, matching the
# same "kept separate" reasoning `block_subagent_destructive_action.
# _check_powershell` states for its own PowerShell leg.
# ---------------------------------------------------------------------------


def _check_bump_outside_repo_write_powershell(
    cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """PowerShell-dialect leg. Detects ONLY the cmdlet-shaped write-sink
    table `_write_bump_sink_shapes.PS_WRITE_SINK_CMDLETS` names (`New-Item`/
    `Set-Content`/`Add-Content`/`Copy-Item`/`Move-Item`/`Out-File`/
    `Tee-Object`) -- the single gap C3's triage (guard-dialect-coverage.md
    row 15) found genuinely unmatched. Every other PowerShell shape in a
    given command -- a bare `>`/`>>` redirect, a `cp`/`mv` alias, an
    unrecognized cmdlet, or a segment this dialect's tokenizer cannot parse
    at all -- records SILENT and is never treated as "inspected and clean",
    per this plan's "prefer SILENT to a guess" posture (mirrors
    `block_subagent_destructive_action._evaluate_powershell_destructive`'s
    own SILENT-on-unmatched-shape discipline).

    TRACKS `Set-Location`/`cd`/`sl`/`chdir` ACROSS SEGMENTS (fixed
    2026-08-08, parity fix mirroring `bump_foreign_repo_write.py`'s own
    `_check_bump_foreign_repo_write_powershell`, which closed the identical
    gap for backlog row
    `2026-08-07-bump-foreign-repo-write-s-powershell-leg-3254b856d676`), and
    the bash body's own `effective_cwd` tracking
    (`_iter_write_sink_candidates`'s `cd` branch) -- one mechanism mirrored,
    not a second invented: a running `effective_cwd` is threaded across
    segments, updated by each resolvable `Set-Location`/alias target
    (`extract_set_location_target_powershell`), so a cmdlet write-target
    candidate extracted AFTER a directory change resolves against the
    changed base, not the original `cwd`.

    SILENT-NOT-DENY IS ABSOLUTE HERE: once a `Set-Location` target fails to
    resolve (`_resolve_relative` returns `None` -- an unresolvable literal,
    a `$var`-shaped or otherwise unexpanded target), `effective_cwd` is left
    UNTRUSTED for the remainder of this command: every subsequent
    write-sink candidate is declined outright (never judged, never a
    guessed base) rather than resolved against a now-possibly-stale cwd.
    This guard's predicate is the INVERSE of `bump-foreign-repo-write`'s
    (fires when a target resolves under NO git root at all, not under a
    DIFFERENT one) but the cwd-tracking machinery is identical; only the
    verdict computed from the resolved `target_dir` differs, and that
    verdict logic below is untouched by this fix.

    Only LITERAL `Set-Location` targets are followed -- a target containing
    `$` (a PowerShell variable, `$env:` reference, or `$(...)`
    subexpression) is NEVER handed to `_resolve_relative` at all, since
    composing it onto `effective_cwd` would silently manufacture a
    wrong-but-plausible-looking base. It is treated identically to an
    `_resolve_relative`-unresolvable target: `effective_cwd` goes untrusted
    and every later candidate in this command is declined. A bare
    `Set-Location`/`cd` with no argument (real PowerShell's `$HOME` default,
    which this module has no reliable way to resolve) is treated as "cwd
    unchanged", never as "cwd now unknown" -- see
    `extract_set_location_target_powershell`'s own docstring.
    """
    if not cmd or not cmd.strip() or not session_id:
        return None

    env = os.environ

    if not bump_applies(session_id, cwd=cwd, env=env):
        return None

    if not session_anchor_has_git_repo(session_id, cwd=cwd, env=env):
        return None

    anchor = resolve_launch_anchor(session_id, cwd=cwd, env=env)
    if not anchor:
        return None
    anchor_gitdir = resolve_gitdir(anchor)
    if anchor_gitdir is None:
        return None

    agent_id = payload.get("agent_id") or "" if isinstance(payload, dict) else ""
    anchor_git_root_str = str(anchor_gitdir.parent) if anchor_gitdir.name == ".git" else str(anchor_gitdir)
    effective_sid = effective_session_id(session_id, anchor_git_root_str, agent_id)

    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name="bump-outside-repo-write")
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
                    "bump-outside-repo-write",
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
                    "bump-outside-repo-write",
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
            probe_dir = _nearest_existing_ancestor(target_dir)
            if probe_dir is None:
                continue
            target_gitdir = resolve_gitdir(probe_dir)
            if target_gitdir is not None:
                continue

            if _target_is_always_allowed(probe_dir, anchor_git_root_str, effective_sid, env=env):
                continue

            if bump_is_cleared(anchor, session_id, git_root=anchor_git_root_str, agent_id=agent_id):
                continue

            agent_class = resolve_agent_class(payload if isinstance(payload, dict) else {}, anchor_git_root_str)
            sandbox_root = (
                _sandbox_root(anchor_git_root_str, effective_sid) if agent_class == AGENT_CLASS_SUBAGENT else ""
            )

            destination_class = (
                DESTINATION_PUBLISH
                if target_is_publish_destination(target_dir, env=env)
                else DESTINATION_FOREIGN
            )
            destination_owner = (
                publish_destination_owner(target_dir, env=env)
                if destination_class == DESTINATION_PUBLISH
                else ""
            )

            record_applicability_event(
                session_id,
                repo=anchor_git_root_str,
                target=target_dir,
                agent_class=agent_class,
                cwd=cwd,
            )

            message = render_bump_message(
                agent_class=agent_class,
                target_repo="no git repo (%s)" % target_dir,
                session_repo=anchor_git_root_str,
                gitdir=anchor_gitdir,
                session_id=effective_sid,
                sandbox_root=sandbox_root,
                destination_class=destination_class,
                destination_owner=destination_owner,
                raw_target=raw_target if raw_target != target_dir else "",
            )
            return _deny(message)

    if not matched_any_cmdlet and not cwd_unresolved:
        record_silent(
            "bump-outside-repo-write",
            "PowerShell leg matched no recognized cmdlet write-sink verb "
            "(New-Item/Set-Content/Add-Content/Copy-Item/Move-Item/Out-File/"
            "Tee-Object) -- redirection operators, cp/mv aliases, and any "
            "other PowerShell shape are declined rather than guessed at "
            "(see this function's own docstring)",
        )

    return None
