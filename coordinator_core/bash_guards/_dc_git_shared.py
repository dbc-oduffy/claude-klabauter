"""Git argv, cwd and commit-operand helpers read by two or more dispatch_checks guard
families: `git -C` / `cd` prefix resolution, the PowerShell git-bypass segment walk,
hazard-repo and load-bearing-path predicates, and the `git commit` operand scan.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.bash_guards import _dc_git_probe
from coordinator_core.bash_guards._command_tokenizer import (
    token_matches_binary as _bt_token_matches_binary,
)
from coordinator_core.bash_guards._dc_shell_text import (
    _bt_is_bare_redirection_token,
    _bt_is_redirection_token,
    _strip_q,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    _strip_ps_quotes,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._rewrite_support import (
    _GIT_GLOBAL_OPT_NO_ARG_SIMPLE,
    _GIT_GLOBAL_OPT_WITH_ARG,
    _bt_git_subcommand_start_index,
    _bt_peel_wrapper_prefix,
)
from coordinator_core.bash_guards.block_subagent_destructive_action import _ps_normalize_verb_token


#: BX-13 fix (2026-07-29, confirmed live via the real dispatcher, not a
#: theoretical hole): `git` used to have to sit DIRECTLY after the segment
#: boundary (`^|&&|;|\|`) with nothing but whitespace in between, so a
#: leading environment-variable assignment, an `env` prefix, or a passthrough
#: wrapper (`nice`/`time`/`command`/`exec`/`nohup`/`ionice`/`timeout`/
#: `stdbuf`/`which`/`type`) -- `FOO=1 git commit --no-verify -m x`,
#: `nice git commit --no-verify -m x` -- was never recognized as a git
#: invocation at all and sailed through this guard while still executing
#: `--no-verify` for real. This tolerates the same wrapper-prefix shape this
#: file's own `_GC_CLEAN_CMD_RE` (destructive-git-clean) already tolerates
#: ahead of its own `git` anchor, reused here rather than re-derived.
#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): the walk
#: above tolerated the WRAPPER BINARY token itself but never the wrapper's
#: OWN argument(s) -- `timeout 30 git commit --no-verify`, `ionice -c2 git
#: commit --no-verify`, `stdbuf -oL git commit --no-verify` all landed on
#: `30`/`-c2`/`-oL` as the "next token", which is neither a wrapper word nor
#: `git`, so the walk broke there and the segment fell through to the raw
#: `_BYPASS_RE` scan below -- which has the identical gap (a bare
#: wrapper-word-then-space is required immediately before `git`, with no
#: allowance for an intervening argument token). `_WRAPPER_FLAG_GROUP` below
#: tolerates zero or more of the wrapper's own `-flag` tokens, both the
#: attached-value form (`-c2`/`-oL`, a single token) and the SEPARATE-token
#: form (`-c 2`) via a negative lookahead (`(?!git\b)`) on the optional value
#: slot -- a bare flag can never swallow the literal `git` token that must
#: follow as its "value"; the regex engine backtracks off the optional value
#: group rather than consuming past the command boundary.
#: Catastrophic-backtracking fix (2026-08-05, dispatch-latency-bound corpus):
#: `_WRAPPER_FLAG_GROUP`'s star and `_BYPASS_PREFIX`'s outer star (below) both
#: sit ABOVE an alternation whose branches can match overlapping text -- the
#: `env\s+(?:\S+=\S*\s+)*` branch and the bare-assignment branch
#: (`[A-Za-z_][A-Za-z0-9_]*=\S*\s+`) both recognize the same
#: assignment-shaped token, so a long run of `FOO=1 BAR=2 ...` with no
#: trailing `git` gives the engine exponentially many ways to partition that
#: run across outer-star iterations before it can conclude no match exists --
#: the textbook nested-quantifier ReDoS shape. Wrapped in `(?>...)` (atomic
#: group, Python 3.11+ -- this package's floor, see repo CLAUDE.md) rather
#: than rewritten to remove the overlap: atomic grouping commits to the
#: first successful partition and never reconsiders it, which is safe here
#: specifically BECAUSE every alternative below ends at a whitespace
#: boundary and NONE of them can match a literal `git` token -- there is no
#: legitimate input for which some OTHER partition of the prefix would let
#: the trailing `git` match succeed where the first-found partition's
#: trailing `git` match fails. Losing the ability to backtrack into the
#: prefix therefore cannot lose a match; it only forecloses the exponential
#: re-exploration of partitions that were never going to matter.
_WRAPPER_FLAG_GROUP = r"(?>(?:-\S+(?:\s+(?!git\b)\S+)?\s+)*)"
_BYPASS_PREFIX = (
    # BRACE-GROUPING FIX (2026-07-29, EM-run second-pass integration,
    # confirmed live): `{ ` is bash's brace-list opener (a reserved word
    # requiring a following space, not an operator like `(`), so
    # `{ git add -A; }` / `{ git commit --no-verify; }` reach this prefix
    # with a leading `{ ` this group did not previously tolerate -- any
    # caller anchoring on `_BYPASS_PREFIX` (this regex fallback and
    # `check_blanket_git_add`'s matcher) silently missed the brace-wrapped
    # form. Added as one more zero-or-more alternative, same as every other
    # wrapper here.
    # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus pass,
    # confirmed live): `( git add -A )` / `( git commit --no-verify )` were
    # never tolerated either -- `(` is whitespace-separated in the tested
    # shape and, like `{`, the shared tokenizer (`shlex.shlex(...,
    # punctuation_chars=";&|")`, `_command_tokenizer.py`) does not treat it
    # specially, so it always falls out as its own token via ordinary
    # whitespace-splitting. Same bypass class, same fix shape as the brace
    # alternative directly above -- this regex fallback and
    # `check_blanket_git_add`'s matcher both anchor on `_BYPASS_PREFIX`.
    r"(?>(?:\{\s+|"
    r"\(\s+|"
    r"(?:sudo|command|time|exec|nohup|which|type)\s+|"
    r"nice\s+" + _WRAPPER_FLAG_GROUP + r"|"
    r"timeout\s+" + _WRAPPER_FLAG_GROUP + r"\d+(?:\.\d+)?[smhd]?\s+|"
    r"ionice\s+" + _WRAPPER_FLAG_GROUP + r"|"
    r"stdbuf\s+" + _WRAPPER_FLAG_GROUP + r"|"
    r"env\s+(?>(?:\S+=\S*\s+)*)|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)"
)


def _ps_git_bypass_segments(
    cmd: str,
    guard_name: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[List[str]]:
    """Shared PowerShell anti-bypass surface for the git-shaped Bucket A
    checks (`destructive-git-orphan`, `destructive-git-clean`,
    `blanket-git-add` -- C3, `docs/plans/2026-08-26-the-destructive-core-
    learns-the-shell-it-guards.md`).

    GATED ON THE DECLARED DIALECT (AC2a retired, 2026-08-26). This scan used
    to run regardless of `tool_name`, and that was FORCED rather than chosen:
    coordinator-content-repo's `_rearm_command_tool_name` rewrote a genuine PowerShell
    payload to `tool_name: "Bash"` before dispatch, so gating here would have
    left this surface blind on exactly the relabeled row the plan's § Problem
    measured as load-bearing. The cost was a measured Bash-leg divergence --
    `` g`it clean -fdx `` was silent at pre-change HEAD and denied after --
    recorded as AC2a's ratified deviation with the removal of that relabel
    named as its re-open trigger. DoE's D1 removed it (`47f4aedfe`; their
    `coordinator/tests/test_preuse_bash_dispatch_rearm.py` now asserts the
    symbol's ABSENCE), so a payload declaring `"Bash"` is once again evidence
    that the caller really used bash, and gating is both possible and correct.

    Returns `[]` -- never `None` -- for a non-PowerShell payload: `None` is
    reserved for "PowerShell text that failed to parse", which callers treat
    as AC4's fail-open silence. Conflating the two would work today only
    because every caller writes `or []`, and would break the moment one of
    them started distinguishing them.

    An ABSENT payload still scans. Direct callers (tests, and any future
    in-process caller that has a command but no hook envelope) declare no
    dialect at all, and for them the pre-D1 posture -- scan and let the
    bash-shaped ladder judge -- is the conservative one. This is deliberately
    not a `.get("tool_name", "Bash")` default, which would silently convert
    "no envelope" into a positive claim of bash.

    Per that plan's own body: git's own argv is byte-identical across
    dialects (the 2026-08-07 audit's "foreign-binary-argv" refinement), so
    none of these three checks needs new VOCABULARY -- their existing
    bash-shaped regex ladders already recognize `git ... clean`/`git reset
    --hard`/`git add -A` the moment that text is visible in plain form.
    What is missing is recovering that plain-text rendering from
    UNDERNEATH PowerShell's own syntax: the `&` call operator, `Start-
    Process`, backtick escapes, and `@'...'@`/`@"..."@` here-strings.
    `resolve_segments_for_dialect` already resolves all four for free --
    `&` is emitted as a literal separator token by `_powershell_tokens`
    (module docstring, "leading & call-operator becomes literal token
    `&`"), `Start-Process`'s hidden argv is reconstructed by
    `expand_start_process_invocations` (called unconditionally on the
    POWERSHELL leg inside `resolve_segments_for_dialect`), and backtick
    escapes / here-strings are native `tree-sitter-pwsh` grammar, not
    something this function has to re-implement. `Invoke-Expression`/`iex`
    is the one shape that seam does NOT unwind on its own -- its string
    argument is DATA to the tokenizer, not argv -- so it is unwrapped one
    level deep here, mirroring this module's own `_shell_c_unwrap_
    payloads` idiom for `sh -c`/`bash -c`.

    Returns `None` when `resolve_segments_for_dialect` itself returns
    `None` (the PowerShell text failed to parse) -- distinct from `[]`
    ("parsed fine, no git invocation found") -- so every caller can
    preserve AC4's fail-open posture: unparseable PowerShell must yield
    the SAME silence it yields today, never a deny. Each returned string
    is a bash-shaped `git ...` rendering, appended by the caller onto the
    SAME string-segment list its pre-existing regex ladder already walks
    (`_split_segments(cmd)` / `_awk_quote_aware_split(cmd)`), reusing that
    ladder byte-for-byte instead of forking a parallel PowerShell copy of
    it -- same shape `check_destructive_rm` already established for its
    own PowerShell leg.
    """
    if payload is not None and dialect_from_tool_name(
        payload.get("tool_name")
    ) is not Dialect.POWERSHELL:
        return []

    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name=guard_name)
    if segments is None:
        return None
    out: List[str] = []
    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        head = _ps_normalize_verb_token(tokens[0]).lower()
        if head in ("invoke-expression", "iex") and len(tokens) > 1:
            inner = _strip_ps_quotes(tokens[1])
            nested = resolve_segments_for_dialect(inner, Dialect.POWERSHELL, guard_name=guard_name)
            if not nested:
                continue
            for ntoks, _npipe in nested:
                if not ntoks:
                    continue
                nhead = _ps_normalize_verb_token(ntoks[0]).lower()
                if nhead == "git":
                    out.append(" ".join([nhead] + [str(t) for t in ntoks[1:]]))
            continue
        if head == "git":
            out.append(" ".join([head] + [str(t) for t in tokens[1:]]))
    return out


def _new_git_memo() -> Callable[[List[str], Optional[str]], Tuple[int, str]]:
    """Factory for a per-call `_run_git` memo, keyed on the exact
    `(cwd, args)` pair actually run.

    F0-b (2026-08-05, spawn-count defect): shared by every guard function
    with a per-segment/per-target loop that re-resolves an identical `git`
    query multiple times -- first found in `check_destructive_git_orphan`
    CHECK 1 (`rev-parse --verify`/`rev-list --count` spawned once per
    segment of a chained `git reset --hard`, ~2 spawns/segment, ~1,778
    spawns / 9.9s at 16 KiB) and, same session, `check_destructive_rm`'s
    scratch-allowlist/dirty-work probes (`git -C <parent-dir> rev-parse
    --show-toplevel`/`--git-dir`, spawned once per rm TARGET with
    byte-identical argv whenever two targets share a parent directory --
    ~1 extra spawn per target, linear: `rm a.py b.py c.py` in one dir
    measured 5 spawns before this fix, 4 after).

    Repo state cannot change mid-dispatch (a single synchronous hook
    invocation, no concurrent mutator), so exact-key reuse within ONE call
    into the owning guard function is sound: identical args+cwd are
    guaranteed to return the identical `(rc, stdout)` pair, so this can
    never turn a failed resolution into a cached success or vice versa --
    it is exact-key reuse, not a heuristic.

    Returns a closure over a FRESH dict on every call -- deliberately NOT a
    module-level cache. Each caller (e.g. `check_destructive_git_orphan`,
    `check_destructive_rm`) must call this once per invocation of ITSELF and
    keep the returned closure local to that call, never hoisted to module
    scope: the keys are attacker-influenced (ref names, filesystem paths
    from the command under evaluation), so a shared cache would have
    unbounded growth and would leak resolved verdicts across unrelated
    dispatches / test runs sharing one interpreter.
    """
    memo: Dict[Tuple[Optional[str], Tuple[str, ...]], Tuple[int, str]] = {}

    def _memo_run_git(args: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
        key = (cwd, tuple(args))
        cached = memo.get(key)
        if cached is None:
            cached = _dc_git_probe._run_git(args, cwd=cwd)
            memo[key] = cached
        return cached

    return _memo_run_git


def _extract_git_c_dir(seg: str) -> str:
    m = re.search(r"git\s+-C\s+(\S+)", seg)
    if not m:
        return ""
    return _strip_q(m.group(1))


# Flags whose operand is free-form prose, never an actual command token --
# CHECK 2 must not let the operand's WORDING influence its verdict (a commit
# message that *describes* a force-push is not a force-push). See
# cross-repo/inbox/2026-07-20-claude-central-em-orphan-guard-check2-prose-
# false-positive.md.
_FREETEXT_OPERAND_FLAGS = ("-m", "--message")


def _orphan_c_cwd(c_dir: str, base_cwd: Optional[str] = None) -> Optional[str]:
    """F0: per-segment `git -C <dir>` becomes subprocess `cwd=<dir>` (git -C
    is directory-scoped identically to running the git binary FROM that
    directory for the purposes of this dispatcher's checks). Empty c_dir ->
    None (bare cwd-relative `git rev-parse`, mirrors the bash `GOPT=()` case).

    Bug row: state/bug-backlog/2026-09-12-check-1-skips-verification-on-a-
    relative-c-dir-91b4c7de20a3.yaml. A RELATIVE `c_dir` (`git -C reset` or
    `git -C ./reset`) used to be returned unchanged and handed to the probe
    subprocess as `cwd=`. That resolves against the GUARD PROCESS's own
    directory, not the invoking session's, so it names the wrong repository
    (or nothing at all) and the `rev-parse --verify`/`rev-list --count`
    probes fail unverifiable -- which this guard's callers then treat as
    "nothing to deny" instead of "could not check". An absolute `-C <dir>`
    naming the identical target was verified correctly the whole time, so
    the bypass was reachable only via the ordinary, non-adversarial spelling
    of a sibling checkout.

    `base_cwd` -- the invoking session's own cwd (payload `cwd`, falling
    back to `git_root`) -- resolves a relative `c_dir` against the directory
    the command actually ran from, matching `-C`'s own resolution rule.
    `base_cwd` unavailable -> the bare pre-fix return, deliberately: probing
    against the WRONG base would answer confidently about a different
    repository's history, which is worse than the unverifiable case this
    guard already fails safely on."""
    if not c_dir:
        return None
    if os.path.isabs(c_dir) or not base_cwd:
        return c_dir
    try:
        return os.path.normpath(os.path.join(base_cwd, c_dir))
    except (OSError, ValueError):
        return c_dir


_BT_LEADING_CD_RE = re.compile(r"^\s*cd\s+(\S+)\s*(?:&&|;)")


def _bt_leading_cd_prefix_cwd(cmd: str, base_cwd: Optional[str]) -> Optional[str]:
    """The effective cwd a repo-root-resolving guard should probe against
    when the COMMAND, not the guard process, changes directory first --
    `cd <fixture> && git ...`.

    Bug row: state/bug-backlog/2026-08-27-destructive-git-and-scope-guards-
    resolve-295928a71726.yaml. A guard resolving `os.getcwd()` (the guard
    process's own cwd) or the payload's session-level `cwd` reads the WRONG
    repo whenever the executed command's own leading `cd <dir> &&` moves it
    elsewhere -- observed both directions: a scope guard warning about a
    path from an unrelated repo, and a destructive-checkout guard blocking a
    harmless command on a foreign repo's state.

    NARROW BY DESIGN, matching the evidenced shape only: a `cd <dir>` at the
    very START of the command, immediately followed by `&&` or `;`. This
    does NOT track cwd across an arbitrary chain of segments (`git ... && cd
    x && git ...`) -- that is `_bt_blanket_add_dash_c_cwd`'s sibling-shape
    gap for a FUTURE widening, not this fix. Reuses that function's `-C`
    resolution shape (last value wins is not applicable here since only the
    leading form is matched at all).

    Returns `None` -- never raises, never denies more -- when no leading
    `cd` is present, the directory token cannot be resolved (a glob or an
    unexpanded variable), or `base_cwd`/`os.getcwd()` resolution fails; the
    caller falls back to its own prior `cwd` in every such case. A guard
    whose root resolution only ever narrows on a hit and never widens on a
    miss cannot turn an unrelated command into a false deny."""
    m = _BT_LEADING_CD_RE.match(cmd)
    if not m:
        return None
    raw = m.group(1).strip("'\"")
    if not raw or "*" in raw or "?" in raw or raw.startswith("$"):
        return None
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    try:
        return os.path.normpath(os.path.join(base_cwd or os.getcwd(), raw))
    except (OSError, ValueError):
        return None


_BT_PURE_CD_SEGMENT_RE = re.compile(r"^\s*cd\s+(\S+)\s*$")


def _bt_cd_chain_cwds_by_segment(
    segments: List[str], base_cwd: Optional[str]
) -> List[Optional[str]]:
    """For EVERY index in ``segments``, the effective cwd once every prior
    segment that is PURELY ``cd <dir>`` (nothing else on that segment) has
    been applied in order -- leaving cwd unchanged across an intervening
    non-``cd`` command (``rm``, ``mkdir``, an earlier ``git`` call, ...).
    One forward pass, so a caller needing this for every segment of a
    command (the `check_destructive_git_orphan` loop) stays linear in
    segment count -- calling a single-index version of this per segment
    would turn one segment walk into an O(n^2) rescan on a many-segment
    command.

    Bug row: state/bug-backlog/2026-09-11-destructive-git-orphan-judges-a-
    reset-ha-096ab8220a8a.yaml. `_bt_leading_cd_prefix_cwd` only follows a
    `cd` at the very START of the command, so a repro of the shape `cd
    <scratch> && rm -rf x && mkdir x && cd x && git reset --hard <ref>`
    resolves only the OUTER `cd <scratch>` -- never the inner `cd x` where
    the target repo actually lives -- and this guard's per-segment probe
    falls through to the guard process's own cwd, judging a reset against
    the wrong repository.

    Widens `_bt_leading_cd_prefix_cwd`'s SAME shape (narrow on a miss, never
    guess) across the whole chain instead of one leading segment: an
    unresolvable `cd` target (a glob, an unexpanded `$VAR`) POISONS every
    later index for the rest of the command and each becomes `None` rather
    than guessing past it -- a later literal `cd` cannot be trusted once an
    earlier hop in the same chain is unknown, since it resolves relative to
    that unknown directory. An index with no prior pure `cd` segment at all
    is also `None`, matching `_bt_leading_cd_prefix_cwd`'s no-match return
    so callers can OR the two fallbacks together uniformly."""
    out: List[Optional[str]] = []
    cur: Optional[str] = None
    poisoned = False
    for seg in segments:
        out.append(None if poisoned else cur)
        if poisoned:
            continue
        m = _BT_PURE_CD_SEGMENT_RE.match(seg)
        if not m:
            continue
        raw = m.group(1).strip("'\"")
        if not raw or "*" in raw or "?" in raw or raw.startswith("$"):
            poisoned = True
            continue
        if os.path.isabs(raw):
            cur = os.path.normpath(raw)
            continue
        try:
            cur = os.path.normpath(os.path.join(cur or base_cwd or os.getcwd(), raw))
        except (OSError, ValueError):
            poisoned = True
    return out


def _bt_cd_chain_cwd_before(
    segments: List[str], idx: int, base_cwd: Optional[str]
) -> Optional[str]:
    """Single-index convenience wrapper over
    `_bt_cd_chain_cwds_by_segment` -- see that function's docstring for the
    batch shape a many-segment caller needs instead of this one, called
    once per segment."""
    return _bt_cd_chain_cwds_by_segment(segments[: idx + 1], base_cwd)[idx]


_GC_LOADBEARING_RE = re.compile(
    r"(^|/)state(/|$)|(^|/)docs/plans(/|$)|(^|/)docs/decisions(/|$)|"
    r"(^|/)docs/wiki(/|$)|(^|/)docs/research(/|$)|"
    r"(^|/)cross-repo/(inbox|archive|outbox)(/|$)|-handoff.*\.md$|"
    r"(^|/)completion(/|$)|review-trail/.*\.json$"
)
_GC_LOADBEARING_PREFIXES = (
    "state", "docs/plans", "docs/decisions", "docs/wiki", "docs/research",
    "cross-repo/inbox", "cross-repo/archive", "cross-repo/outbox",
)


def _is_loadbearing(path: str, seam_state_root: str = "") -> bool:
    p = path.rstrip("/")
    if not p:
        return False
    if _GC_LOADBEARING_RE.search(p):
        return True
    for prefix in _GC_LOADBEARING_PREFIXES:
        if prefix == p or p.startswith(prefix + "/"):
            return True
    if seam_state_root:
        if p == seam_state_root or p.startswith(seam_state_root + "/"):
            return True
        if seam_state_root.startswith(p + "/"):
            return True
    return False


def _meta_repo_root() -> Optional[str]:
    """Resolve the ~/.claude meta-repo root path (the guard's original,
    unwidened scope), or ``None`` if it cannot be resolved (e.g. no ``HOME``
    in the environment) -- callers treat ``None`` as "this half of the
    discriminator did not match," never as an error to propagate."""
    try:
        return os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude"))
    except OSError:
        return None


def _hazard_registry_repo_roots() -> List[str]:
    """Every non-empty ``repos.*`` value in the machine-local fleet
    registry (``registry.local.toml`` merged over ``registry.toml``, same
    two-file precedence as ``machine_resolver.registry_get`` -- see that
    module for the registry-directory resolution ladder, including its
    ``MACHINE_LOCAL_REGISTRY_DIR`` test-isolation override). These are the
    fleet sibling repos THIS machine knows about -- an OSS-consumer install
    has no such registry, so this returns ``[]`` there, not a guess.

    Fails open: any exception reading/parsing the registry (missing
    settings home, malformed TOML, an unreadable file) returns ``[]``
    rather than raising -- an unresolvable registry can only narrow what
    counts as a hazard repo, never turn an unrelated command into a deny.
    """
    try:
        from coordinator_core import machine_resolver

        reg_dir = machine_resolver.registry_dir()
        roots: List[str] = []
        for fname in ("registry.local.toml", "registry.toml"):
            flat = machine_resolver.load_flat_registry_file(reg_dir / fname)
            for key, val in flat.items():
                if key.startswith("repos.") and val:
                    roots.append(str(val))
        return roots
    except Exception:  # noqa: BLE001 -- fail-open by design, see docstring
        return []


def _paths_match(a: str, b: str) -> bool:
    """``os.path.samefile`` when both paths exist on disk (matches real
    inodes across symlinks/mount points), falling back to a normalized
    string comparison when either side cannot be ``stat``-ed (a registry
    entry pointing at a path that no longer exists on this machine, etc.).
    Mirrors the pre-widening samefile-with-string-fallback shape this guard
    already used for its ~/.claude comparison."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _is_hazard_repo(git_root: str) -> bool:
    """The hazard discriminator: True iff ``git_root`` is a repo where a
    blanket ``git add -A`` can plausibly sweep a CONCURRENT session's
    in-flight edits into this commit -- the ~/.claude meta-repo (the origin
    incident's own repo), or any repo this machine's fleet registry
    (``repos.*``) tracks (claude-klabauter, coordinator-content-repo, and every other
    sibling repo on this machine -- see ``_hazard_registry_repo_roots``).
    An arbitrary OSS-consumer repo has neither property, so this returns
    False there and the guard stays a no-op -- restoring the load-bearing
    percolation property the 2026-07-31 all-repo widening (``2d6bca4e``)
    destroyed (see this section's own header comment for the incident).

    FAILS OPEN: wrapped in a blanket ``except Exception`` -- ANY failure
    classifying ``git_root`` (a broken ``HOME``, an unreadable settings
    home, a registry read raising something ``_hazard_registry_repo_roots``
    itself did not anticipate) returns False, i.e. "not a hazard repo," so
    ``check_blanket_git_add`` ALLOWS rather than denies. A guard that denies
    when it cannot classify is exactly the defect this discriminator
    replaces -- unlike the pre-2026-07-31 gate's IMPLICIT fail-open (a
    ``samefile`` ``OSError`` fell through to a plain string-equality
    fallback, never to a deny), this is now an explicit, load-bearing
    contract of the function.
    """
    try:
        meta = _meta_repo_root()
        if meta is not None and _paths_match(git_root, meta):
            return True
        for root in _hazard_registry_repo_roots():
            if _paths_match(git_root, root):
                return True
        return False
    except Exception:  # noqa: BLE001 -- fail-open by design, see docstring
        return False


#: `git commit` options whose VALUE is a separate following token -- skipped
#: as a pair by `_bt_commit_has_explicit_pathspec` so an option value can
#: never be misread as the `--` pathspec separator (nor a pathspec-looking
#: value be mistaken for scope).
#:
#: `-S`/`--gpg-sign` are deliberately ABSENT (review finding, P0): both are
#: OPTIONAL-argument git flags whose value must be ATTACHED (`-Skeyid`,
#: `--gpg-sign=keyid`) -- a standalone `-S`/`--gpg-sign` token consumes
#: NOTHING, so bucketing them here caused unconditional two-token
#: consumption that swallowed the NEXT real token (e.g. `-m`) as a phantom
#: value. `git commit -S -m x` then misread the commit MESSAGE as a
#: pathspec, silently narrowing/suppressing the sweep warning -- a
#: false-suppression, the worst-case direction under bound 4 below.
#: Standalone `-S`/`--gpg-sign` now falls through to the generic
#: short-cluster/long-flag handling in `_bt_commit_operand_scan`, which
#: marks the parse `ambiguous` (fails open to advisory, never to
#: suppression) exactly as the short-cluster docstring below already
#: claims for `-S`. The ATTACHED forms (`-Skeyid`, `--gpg-sign=keyid`) are
#: handled explicitly in `_bt_commit_operand_scan` as single, non-ambiguous
#: tokens.
_GIT_COMMIT_OPT_WITH_ARG = frozenset(
    {
        "-m", "--message", "-F", "--file", "-c", "--reedit-message",
        "-C", "--reuse-message", "--fixup", "--squash", "--author",
        "--date", "--cleanup", "--trailer", "--pathspec-from-file",
        "-t", "--template",
    }
)


#: Long `git commit` options taking NO value. Anything outside this set (and
#: outside `_GIT_COMMIT_OPT_WITH_ARG`) is an option this parser cannot prove
#: does not consume the following token -- SC-DR-020's bound 4 requires that
#: to fail OPEN (no suppression), never to be assumed operand-less.
_GIT_COMMIT_LONG_NO_ARG = frozenset(
    {
        "--all", "--patch", "--signoff", "--no-signoff", "--verbose",
        "--quiet", "--no-verify", "--verify", "--no-post-rewrite", "--amend",
        "--no-edit", "--edit", "--allow-empty", "--allow-empty-message",
        "--status", "--no-status", "--dry-run", "--short", "--branch",
        "--no-branch", "--porcelain", "--long", "--null", "--reset-author",
        "--interactive", "--only", "--include", "--no-gpg-sign",
        "--pathspec-file-nul",
    }
)

#: Short `git commit` flag letters taking NO value, for bundled-cluster
#: parsing. `m` (consumes the message), `S` (OPTIONAL keyid), and `u`
#: (OPTIONAL mode) are deliberately absent -- an optional-argument flag
#: cannot be resolved statically, so it renders the parse ambiguous.
_GIT_COMMIT_SHORT_NO_ARG = frozenset("apsvqnezoi")


#: A bundled short-flag cluster ending in `m` (`-am`, `-sm`, `-asm`) -- the
#: only bundled position where the message operand follows as its own token.
_BUNDLED_SHORT_M_RE = re.compile(r"^-[a-zA-Z]*m$")


def _bt_is_shattered_operand(tok: str) -> bool:
    """True iff `tok` is a fragment of a shattered quoted message rather than
    a pathspec -- it carries a newline.

    Same defect class as the surviving-redirection fix above, at the same
    polarity: shell syntax the tokenizer failed to consume, counted as an
    operand, makes an UNSCOPED `git commit` read as explicitly scoped and
    silences the checks that exist to catch it.

    The residue here is a quoted message rather than a redirection. A
    heredoc-and-command-substitution message (`-m "$(cat <<'EOF' ... EOF
)"`)
    whose BODY contains a double quote closes the outer quote early, so
    `shlex.split` shatters the message into several bare tokens -- and the
    tail fragment carries the heredoc terminator and its newlines. A
    command-line pathspec never does: git receives argv from the shell, and
    a newline inside an operand means the operand is not what the caller
    wrote. Verified live against the shape a peer hit
    (`state/bug-backlog/2026-08-27-bare-git-commit-inside-engine-python-has-
    no-guard-4b1e77c9d3a2.yaml`): a genuinely BARE `git commit -m "<message
    with an inner quoted phrase>"` scanned as carrying two positional
    pathspecs and suppressed the bare-commit deny entirely.

    A path CAN legally contain a newline on disk. It is still right to
    refuse to read one here: this walk cannot distinguish that path from a
    shattered message, and per SC-DR-020 bound 4 an unprovable operand list
    must render the parse ambiguous, which fails OPEN to the advisory. The
    cost of guessing wrong in the other direction is a peer's swept work.
    """
    return chr(10) in tok or chr(13) in tok


def _bt_commit_operand_scan(
    seg_tokens: List[str],
) -> Tuple[List[str], bool, bool, bool]:
    """Walk a `git commit` segment once and report
    ``(positional_operands, ambiguous, has_include, has_only)``.

    `has_only` is resolved HERE rather than by a caller re-scanning raw
    tokens, because only this walk performs option-value pair-skipping: a
    caller matching `-o` across bare tokens reads the MESSAGE in
    `git commit -m -o` as the `--only` flag and fakes scope out of it.

    The single parser behind BOTH `--`-keyed predicates below. SC-DR-020
    (`coordinator-content-repo coordinator/docs/wiki/scoped-safety-commits.md`, token
    `SEPARATOR-IS-DISAMBIGUATION-NOT-SCOPE`) rules that `--` is git's
    revision/path DISAMBIGUATION token and carries no scope of its own:
    `git commit a.py -m x` and `git commit -m x -- a.py` are the same
    operation. A guard whose verdict turns on that token reads syntax where
    it means to read semantics -- the defect this module already hit at
    opposite polarity in the deleted `git add` pathspec extractor (deny keyed ON the
    separator) and here (suppression keyed on it).

    `ambiguous` is True when the walk cannot prove the operand list: an
    option outside the known no-arg/with-arg sets may or may not consume the
    next token, and an optional-argument flag (`-S`, `-u`) cannot be resolved
    statically at all. Per SC-DR-020 bound 4 the asymmetry is deliberate --
    callers must fail OPEN to advisory on ambiguity, never to suppression: a
    spurious advisory costs attention, a spurious suppression costs a peer's
    work.

    Operands after a standalone `--` are returned as operands too, so a
    caller never needs to re-walk for the separator form.

    A surviving shell redirection token (`_bt_is_redirection_token`, e.g.
    `<<'MSG'`, `>/dev/null`, `2>&1`, `<file`) is NEVER counted as an operand,
    whether bare-positional or after `--` (2026-08-05 cockpit-incident fix):
    it is shell syntax the earlier redirection-stripping pass failed to
    remove, not a git pathspec, and treating it as one made an UNSCOPED
    `git commit` carrying any redirection read as explicitly scoped --
    silencing both `_bt_commit_has_explicit_pathspec` and (via this same
    walk) `_bt_commit_own_pathspec` on exactly the shape they exist to
    catch. A whitespace-separated redirection (`> /dev/null`, `2> log`,
    `< input`) tokenizes as a BARE operator token followed by a separate
    target token; `_bt_is_bare_redirection_token` distinguishes this case
    and the walk below also skips that following target token, or it
    would survive into `operands` untouched (2026-08-05 follow-up
    review, Finding 1).

    `-S`/`--gpg-sign` special-case (review finding, P0): both are
    OPTIONAL-argument flags whose value must be ATTACHED. A standalone `-S`
    or `--gpg-sign` token consumes nothing and cannot be resolved
    statically -- it renders the parse `ambiguous`, handled by the
    `--gpg-sign`/`-S`-absent `_GIT_COMMIT_OPT_WITH_ARG` (see that
    frozenset's docstring) falling through to the generic long-flag /
    short-cluster arms below. The ATTACHED forms (`-Skeyid`,
    `--gpg-sign=keyid`) are single tokens: `--gpg-sign=keyid` is handled by
    the generic `"=" in tok` long-flag arm below; `-Skeyid` needs its own
    check here because the generic short-cluster arm would otherwise
    decompose `keyid`'s letters as a bundle of unrelated single-char flags."""
    operands: List[str] = []
    ambiguous = False
    has_include = False
    has_only = False
    start_idx = _bt_git_subcommand_start_index(seg_tokens)
    if start_idx is None or seg_tokens[start_idx - 1] != "commit":
        return ([], True, False, False)
    i = start_idx
    n = len(seg_tokens)
    while i < n:
        tok = seg_tokens[i]
        if tok == "--":
            rest = seg_tokens[i + 1:]
            j = 0
            m = len(rest)
            while j < m:
                t = rest[j]
                if _bt_is_redirection_token(t):
                    if _bt_is_bare_redirection_token(t) and j + 1 < m:
                        j += 2
                    else:
                        j += 1
                    continue
                if _bt_is_shattered_operand(t):
                    # After a standalone `--` the parse is NOT in doubt:
                    # everything here is a pathspec by git's own grammar, so
                    # a shattered fragment is dropped without rendering the
                    # walk ambiguous. Only the BARE-positional inference
                    # below has to fail open, because there the fragment is
                    # indistinguishable from the operand it is pretending
                    # to be.
                    j += 1
                    continue
                operands.append(t)
                j += 1
            break
        if tok in _GIT_COMMIT_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--"):
            base = tok.split("=", 1)[0]
            if base == "--include":
                has_include = True
            elif base == "--only":
                has_only = True
            elif "=" not in tok and tok not in _GIT_COMMIT_LONG_NO_ARG:
                ambiguous = True
            i += 1
            continue
        if tok.startswith("-S") and tok != "-S":
            # Attached optional-arg form (`-Skeyid`): a single, resolved
            # token that consumes nothing further and is not a bundled
            # short-flag cluster -- see the docstring above.
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            letters = tok[1:]
            if "i" in letters:
                has_include = True
            if "o" in letters:
                has_only = True
            if _BUNDLED_SHORT_M_RE.match(tok):
                letters = letters[:-1]
                i += 2
            else:
                i += 1
            if any(c not in _GIT_COMMIT_SHORT_NO_ARG for c in letters):
                ambiguous = True
            continue
        if _bt_is_redirection_token(tok):
            if _bt_is_bare_redirection_token(tok) and i + 1 < n:
                i += 2
            else:
                i += 1
            continue
        if _bt_is_shattered_operand(tok):
            # A shattered fragment sitting BEFORE a standalone `--` is
            # pre-separator residue: the tail branch above supplies the real
            # pathspec, so dropping it silently is right and marking the walk
            # ambiguous would deny a correctly-scoped command. With no
            # separator anywhere in the segment there is nothing to fall back
            # on and the operand list is unprovable -- fail OPEN (SC-DR-020
            # bound 4), which is what makes the genuinely-bare shape deny.
            if "--" not in seg_tokens:
                ambiguous = True
            i += 1
            continue
        operands.append(tok)
        i += 1
    return (operands, ambiguous, has_include, has_only)


def _bt_commit_has_explicit_pathspec(seg_tokens: List[str]) -> bool:
    """True iff a `git commit` segment carries an explicit trailing
    `-- <paths>` scope (a standalone `--` separator with at least one
    operand after it), skipping option values so `-m -- ` shapes cannot
    fake one. This is the ratified default scoped-commit form
    (DoE `coordinator-content-repo coordinator/docs/wiki/concurrent-em-git-operations/scoped-safety-commits.md` SC-DR-008/SC-DR-015), so its
    presence is the suppression condition for the advisory below.

    `--pathspec-from-file[=<f>]`/`--pathspec-file-nul` also count as
    explicit scope (C1a Fix 1): per `man git-commit`(1), `--pathspec-from-
    file` is git's documented equivalent of naming the paths on the command
    line, so it selects the same index-bypassing, self-scoped mode as a
    trailing `-- <paths>`. This was UNDECIDED prior to this fix -- the
    option sat in `_GIT_COMMIT_OPT_WITH_ARG` purely so its (space-separated)
    value token was not misread as something else, and not-counting-as-scope
    was a side effect of that placement, not a considered ruling. Two shapes
    handled explicitly because neither was recognized before this fix: the
    `=`-joined form (`--pathspec-from-file=list.txt`) is a SINGLE token that
    never matches `_GIT_COMMIT_OPT_WITH_ARG` (written for the space-separated
    form only); `--pathspec-file-nul` takes no value at all and was absent
    from every option set in this module.

    `-o`/`--only` (bare or bundled, `-om`) counts for the SAME reason, raised
    by coordinator-content-repo-em against the rule the paragraph above states: it selects
    git's identical index-bypassing self-scoped mode. Verified live against
    git 2.50.1 -- `git commit -o a.txt -m x` with a peer's `b.txt` staged
    commits `a.txt` alone and leaves `b.txt` staged. A no-paths `--only` is
    not a hole: git rejects it outright (`fatal: No paths with
    --include/--only does not make sense.`), and the one legal no-paths
    spelling, `--amend --only`, reuses HEAD's tree rather than the index.

    NEGATIVE SPEC -- `-i`/`--include` is the near-neighbour and is NOT scope:
    it MERGES the named paths INTO the staged index and commits the union.
    Verified live on the same repo: `git commit -i -m x a.txt` landed BOTH
    `a.txt` and the peer's staged `b.txt`. That is precisely the sweep this
    advisory exists to catch, so `--include` must keep firing.

    SC-DR-020 (coordinator-content-repo-em, 2026-08-04, `5a5fbe89f`): a BARE POSITIONAL
    pathspec counts too -- `git commit a.py -m x` is the same operation as
    `git commit -m x -- a.py`, verified live on git 2.54.0 (with a peer's
    `b.txt` also staged, `a.py` lands alone and `b.txt` stays staged). The
    separator was never the thing carrying scope. SC-DR-008's `-- <paths>`
    form stays canonical, enforced by prose and by `ceremony.scoped_git_
    commit` being the preferred path -- not by advising a form git already
    treats as safe. The four bounds that survive the change: `--include`
    still suppresses nothing, an operand-less commit still fires
    (SC-DR-017), `--pathspec-from-file` still counts as scope, and an
    AMBIGUOUS parse fails open to the advisory rather than to silence."""
    for tok in seg_tokens:
        if tok == "--pathspec-file-nul" or tok == "--pathspec-from-file" or (
            tok.startswith("--pathspec-from-file=")
        ):
            return True
    operands, ambiguous, has_include, has_only = _bt_commit_operand_scan(
        seg_tokens
    )
    if has_include:
        return False
    # `-o`/`--only` is scope in its own right even with no operands visible
    # to this walk -- kept ahead of the ambiguity bail so the pre-SC-DR-020
    # behaviour for that flag is bit-identical.
    if has_only:
        return True
    if ambiguous:
        return False
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return False
    return bool(operands)


# ---------------------------------------------------------------------------
# C7's escalation predicates for the check below. The compound `git add
# -- <paths> && git commit -m "x"` bare-commit-half denies UNCONDITIONALLY
# (PM ruling 2026-08-30, `_bt_compound_add_bare_commit`) -- the index probe
# that used to gate it is deleted, not suspended: it cost two `git diff
# --cached` spawns on the commit hot path to compute a condition the ruling
# no longer reads. The remaining index probes here serve the SOLO bare
# commit and the `-a`/`--all` sweep, which are different shapes.
# PM Ruling 2: escalate-only, no index-based suppression exists anywhere in
# this plan; `-a`/`-am`/`--all` are excluded from index-based reasoning
# unconditionally.
# ---------------------------------------------------------------------------

#: A bundled short-flag cluster carrying `a` (`-a`, `-am`, `-sa`, `-asm`, ...)
#: or the long form `--all` -- PM Ruling 2, finding 6: these stage every
#: modified TRACKED file from the WORKTREE at commit time, not the index, so
#: a clean-index probe would read "safe" for a command about to sweep
#: everything. Excluded from C7's index-based reasoning unconditionally, and
#: from `_bt_compound_add_bare_commit`'s unconditional deny for the same
#: reason -- that shape is not the bare-commit half.
_COMMIT_BUNDLED_SHORT_RE = re.compile(r"^-[a-zA-Z]+$")


def _bt_commit_has_sweep_all_flag(seg_tokens: List[str]) -> bool:
    """True iff a `git commit` segment carries `-a`/`--all` (bare or bundled
    into a short-flag cluster, e.g. `-am`, `-sa`), stopping at the segment's
    own `--` pathspec separator (a flag can never appear in the pathspec
    tail) and skipping option-with-arg VALUE tokens so a value token can
    never be misread as a bundled flag."""
    i = 0
    n = len(seg_tokens)
    while i < n:
        tok = seg_tokens[i]
        if tok == "--":
            break
        if tok == "--all":
            return True
        if tok in _GIT_COMMIT_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--"):
            i += 1
            continue
        if _COMMIT_BUNDLED_SHORT_RE.match(tok) and "a" in tok[1:]:
            return True
        i += 1
    return False


def _bt_commit_own_pathspec(seg_tokens: List[str]) -> Optional[List[str]]:
    """Extract WHICH staged paths a `git commit` segment will actually take,
    for Check 5 (`check_validate_commit`) to INTERSECT against the full
    staged index -- a DIFFERENT question from
    `_bt_commit_has_explicit_pathspec`'s WHETHER-scoped boolean, and it
    requires its own extractor rather than reusing that boolean as a
    suppressor.

    NEGATIVE SPEC, restated here because it is load-bearing for the caller:
    `_bt_commit_has_explicit_pathspec` answers "did the operator name a
    scope?", not "which staged paths will this commit take?". Using it as a
    suppressor would silently unwarn `git commit -- <a live peer's file>` --
    precisely the class Check 5 exists to catch (a recorded live incident
    shape). This function instead returns the actual pathspec tokens so the
    caller can re-derive the true committed-path set via its own `git diff
    --cached --name-only -- <pathspec>` call (a pathspec may be a directory
    or glob, not only a literal path, so this function's return value is an
    input to that follow-up call, not the final answer by itself). This is
    NOT the "second pathspec predicate" this chunk otherwise forbids -- it
    answers a different question (WHICH paths, not WHETHER scoped).

    Returns `None` when the actually-committed path set is NOT statically
    knowable, or is knowably "the whole index" -- both mean the caller MUST
    fall back to warning on the full staged list, never to silence:

    - `-a`/`--all` (`_bt_commit_has_sweep_all_flag`): stages from the
      WORKTREE at commit time, not the index -- returns `None`
      unconditionally, even if a trailing pathspec is also present.
    - `--pathspec-from-file[=<f>]`/`--pathspec-file-nul`: the path list
      lives in a file (or stdin) this guard does not read -- returns `None`
      (never guesses at file contents).
    - `-i`/`--include` (bare, long, or bundled, e.g. `-si`): MERGES the
      named paths INTO the staged index and commits the union -- returns
      `None` (must NOT narrow; verified live against git 2.50.1 alongside
      `_bt_commit_has_explicit_pathspec`'s own `-i` note).
    - No trailing `-- <paths>` at all: nothing to narrow by -- returns
      `None`.

    - An AMBIGUOUS parse (an unrecognized option, or an optional-argument
      flag like `-S`/`-u`, either of which may consume the next token):
      returns `None`. SC-DR-020 bound 4 -- narrowing on a mis-parsed operand
      list would drop a genuinely-staged foreign path out of the warning,
      which is the failure this whole check exists to prevent.

    Returns the (non-empty) list of pathspec tokens when none of the above
    applies -- this covers `-o`/`--only` (which bypasses the index for those
    paths), the `-- <paths>` separator form, and a BARE POSITIONAL pathspec
    (SC-DR-020: `git commit a.py -m x` is the same operation as
    `git commit -m x -- a.py`; `man git-commit` treats a trailing pathspec
    with neither `-i` nor `-a` identically to `-o`). All three narrow the
    actually-committed set the same way and none needs distinguishing here.
    Sharing `_bt_commit_operand_scan` with the suppression predicate is
    deliberate: two independent `--`-keyed walks is how this module came to
    hold the same defect at both polarities."""
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return None
    for tok in seg_tokens:
        if tok == "--pathspec-file-nul" or tok == "--pathspec-from-file" or (
            tok.startswith("--pathspec-from-file=")
        ):
            return None
    operands, ambiguous, has_include, _has_only = _bt_commit_operand_scan(
        seg_tokens
    )
    if has_include or ambiguous or not operands:
        return None
    return operands


def _bt_git_dash_c_value(tokens: List[str]) -> Optional[str]:
    """Extract the LAST `-C <dir>` global-option value from a (wrapper-
    peeled) `git ...` invocation, so C7's probe runs against the SAME
    repo/cwd the command itself targets rather than the guard process's own
    cwd. Mirrors `_bt_git_resolved_subcommand`'s global-option walk (added
    alongside it rather than folded in, so C1a's landed walk stays
    untouched). Real git chains multiple `-C` tokens (`-C a -C b` == cwd
    `a/b`); this returns only the LAST raw value -- a documented
    simplification, safe because a probe-cwd miss fails OPEN (no deny),
    never fails closed."""
    tokens = _bt_peel_wrapper_prefix(tokens)
    if not tokens or not _bt_token_matches_binary(tokens[0], "git"):
        return None
    i = 1
    n = len(tokens)
    result: Optional[str] = None
    while i < n:
        tok = tokens[i]
        if tok == "-C" and i + 1 < n:
            result = tokens[i + 1]
            i += 2
            continue
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            if tok in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                i += 1
                continue
            break
        break
    return result


def _bt_probe_cwd(
    seg_tokens: List[str],
    payload: Optional[Dict[str, Any]],
    cmd: Optional[str] = None,
) -> Optional[str]:
    """The directory every `git commit` probe in this cascade must run
    against: the command's own `-C` value when it carries one, resolved
    against the PAYLOAD cwd; failing that, a leading `cd <dir> &&`/`;`
    prefix on the same command (when the caller passes `cmd`); and the
    payload cwd alone when neither is present.

    `_bt_git_dash_c_value` answers only the first half, and returns None for
    the overwhelmingly common command that carries no `-C` at all. Passing
    that None straight to `_run_git` does not mean "the command's own repo",
    it means `cwd=None`, i.e. the GUARD PROCESS's directory -- which is
    whatever the harness happened to be sitting in, not the repo the operator
    is committing to. Every probe below therefore read the wrong repository
    for the ordinary case and the right one only for the rare `-C` case,
    inverting which commands the guard can reason about (coordinator-content-repo-em
    cross-repo memo, 2026-09-04: adding a no-op `git -C .` to an otherwise
    identical command flipped allow to deny). `_bt_commit_scope_operand_is_
    sweeping` already resolves against the payload cwd for exactly this
    reason; the probes now share that root.

    A RELATIVE `-C` is joined onto the payload cwd rather than handed over
    raw: real git resolves `-C sub` against the command's own cwd, so
    resolving it against the guard's would name a third directory belonging
    to neither. An absolute `-C` stands alone, and a relative one with no
    payload cwd to anchor it is returned unchanged -- no worse than today,
    and the probes' own fail-open posture covers a miss.

    A `cd <dir> && git commit --amend ...` chain names a repo just as
    explicitly as `-C` does, but `_bt_git_dash_c_value` cannot see it -- it
    inspects only the `git ...` segment's own tokens, never the command
    prefix ahead of it. Left unhandled, the amend-provenance probe (the
    caller `check_git_commit_safe_commit_advise` reaches through the
    aggregating `collect_advisories=True` path once `check_offer_git_c`'s
    own rewrite is bypassed or not taken) reads the PAYLOAD cwd for a
    command that names a different repo entirely -- CEPEF-B9 / V1's finding
    on `docs/plans/2026-09-26-commit-emit-plane-engine-findings.md`, the same
    class of gap `_bt_leading_cd_prefix_cwd` was already written to close for
    other guards (state/bug-backlog/2026-08-27-destructive-git-and-scope-
    guards-resolve-295928a71726.yaml). `cmd` is optional and defaults to
    `None` so every existing caller that has no raw command string handy (or
    no leading-`cd` shape to worry about) keeps today's `-C`/payload-cwd
    behaviour unchanged; only a caller that HAS the raw command string opts
    in by passing it. `_bt_leading_cd_prefix_cwd` itself narrows on a miss
    and never guesses, so this can only narrow a probe's target, never widen
    one into a false deny.
    """
    dash_c = _bt_git_dash_c_value(seg_tokens)
    payload_cwd = (payload or {}).get("cwd")
    if dash_c is None:
        if cmd is not None:
            cd_cwd = _bt_leading_cd_prefix_cwd(cmd, payload_cwd)
            if cd_cwd is not None:
                return cd_cwd
        return payload_cwd
    if os.path.isabs(dash_c) or not payload_cwd:
        return dash_c
    try:
        return os.path.join(payload_cwd, dash_c)
    except (TypeError, ValueError):
        return dash_c


#: Sequencer states in which git itself refuses a scoped commit, mapped to the
#: verb that finishes each one. `git commit -- <paths>` -- the remediation every
#: deny branch below offers -- exits 128 ("cannot do a partial commit during a
#: merge") in all three, so a deny that offers only that shape is a dead end.
_BT_SEQUENCER_CONTINUE_VERBS: Tuple[Tuple[str, str], ...] = (
    ("MERGE_HEAD", "git merge --continue"),
    ("CHERRY_PICK_HEAD", "git cherry-pick --continue"),
    ("REVERT_HEAD", "git revert --continue"),
)


def _bt_git_sequencer_in_progress(
    seg_tokens: List[str], payload: Optional[Dict[str, Any]] = None
) -> Optional[Tuple[str, str]]:
    """Return `(state_name, continue_verb)` when a merge/cherry-pick/revert is
    mid-flight for the repo this command targets, else None.

    Why this exists as a separate probe rather than another clause inside the
    predicates below: those ask "can this command prove the staged content is
    its own?", and during a sequencer operation the question is malformed. The
    index IS the operation's result -- `git merge` wrote it, and git forbids
    narrowing it (`--` pathspec, `--only`, `--include` all refuse). So the
    honest answer is not "unverifiable, therefore deny": there is no scoped
    shape to fall back to, and the existing deny's own remediation cannot run.
    A guard whose remedy is unrunnable in the state that triggered it teaches
    nothing and blocks the only route through -- which is what happened
    finishing the machine-a merge, 2026-08-26 (state/bug-backlog/2026-08-26-the-
    bare-commit-guard-has-no-merge-head-carve-out.yaml).

    Note the peer-sweep concern does NOT survive into merge state the way it
    does for an ordinary bare commit: git refuses to start a second merge in a
    worktree whose index is already mid-merge, so the session that opened this
    one owns it. What a peer can still do is `git add` into the shared index
    mid-merge, which is why the caller downgrades to an advisory rather than
    dropping the signal entirely.

    Fails CLOSED (returns None, preserving the deny) on any probe failure or a
    spent probe budget -- an undetected sequencer state costs the operator one
    confusing deny, while a falsely-detected one would silently retire the
    guard for every ordinary bare commit.
    """
    cwd = _bt_probe_cwd(seg_tokens, payload)
    for state_name, verb in _BT_SEQUENCER_CONTINUE_VERBS:
        rc, out = _dc_git_probe._run_git(["rev-parse", "--git-path", state_name], cwd=cwd)
        if rc != 0:
            return None
        probe = out.strip()
        if not probe:
            return None
        if not os.path.isabs(probe) and cwd:
            probe = os.path.join(cwd, probe)
        try:
            if os.path.exists(probe):
                return (state_name, verb)
        except OSError:
            return None
    return None
