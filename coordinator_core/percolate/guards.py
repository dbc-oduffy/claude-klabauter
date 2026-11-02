"""Declarative guard/assert transform-kind: check specs run at phase boundaries.

Answers "did the percolation run land in the state the consumer's store asserts it
should" -- pre-ci asserts and post-rsync verify checks are the SAME declarative
check-spec shape (§ docs/plans/2026-07-10-percolation-engine-claude-klabauter.md § Transform-kind
taxonomy, `guard` row, A13/A17-A20). Guards run at post-rsync and pre-ci today only --
`engine.py`'s `run_pre_rsync` phase function does not thread `run_guards_for_section`
(its sole responsibility is the preserve-destination-native backup leg), so a
declarative guard entry on a pre-rsync hook-set is currently unreachable; wiring
pre-rsync guard support in is a real gap to open, not something already supported here.
This module contains no consumer-specific paths, patterns, or override-env names --
every check spec's `params` and `override_env` are supplied entirely by the caller
(ultimately store data, a `guardEntry` per § Store schema).

Scope: `run_guards(scope='staged')` runs only `PER_PATH_GUARD_KINDS` and reports every
`STRUCTURAL_GUARD_KINDS` entry as skipped (`GuardResult.skipped`), never as passed.

Check-spec kinds implemented (one dispatch function per kind, one shared runner):
  - `source-dir-absent`: assert a source-tree directory does NOT exist in the target
    (guards against a stale copy of something that should have been fully removed).
  - `prune-stale-paths`: assert a caller-supplied list of paths, if present, get deleted
    (an active corrective action, not merely a check -- mirrors the shell's stale-path
    prune sweep).
  - `dirs-absent` / `dirs-present`: assert a list of directories are absent / present.
  - `forbidden-script-globs`: assert no file under the target matches a caller-supplied
    forbidden-glob set (e.g. a build/dev-only script that must never ship).
  - `no-residual-pattern`: assert no in-surface file's content matches a caller-supplied
    residual regex/pattern (the leak-check -- a codename or path literal that should have
    been fully substituted but wasn't). This is a PER-TARGET composable hook-set member
    (§ Store schema target inheritance/composition), never a global -- a leak-check
    present on some targets but silently absent on others is a false-green trap, not a
    relaxed check (§ chunk C11 v3-amend ca592759).
  - `file-count-delta`: assert the target's file count matches the EFFECTIVE (i.e.
    file-surface-exclusion-netted) source count within a caller-supplied tolerance -- the
    expected count is computed against the surface AFTER file-surface.py's include/exclude
    resolution has already netted out transform-exempt/ignore-listed source files, never
    a raw hand-count of the source tree (§ chunk C11 v3-amend 652add0c). A source file
    intentionally excluded from the surface must not be counted as a "missing" file.
    An observed count of ZERO never passes silently: a run that counted no files at all
    fails unless the entry declares `params['allow_empty']: true`, because a guard that
    observed nothing and a guard that verified something are otherwise indistinguishable
    from outside (§ that check's docstring).
  - `canary-survival`: assert a caller-supplied canary path/content marker survived the
    run unmodified (a smoke-check that the pipeline did not accidentally clobber content
    it should have left untouched).
  - `source-glob-present`: assert the SOURCE tree (not the target) has >=1 file matching
    a caller-supplied basename glob -- a presence check, not a count (`file-count-delta`
    is an awkward fit for "does at least one X exist"). Dispatches against `source_root`,
    a distinct root from every other check kind in this module (all of which operate on
    `target_root`); `run_guard` raises `GuardCheckError` if this kind is requested without
    a `source_root`.
  - `python-syntax-valid`: assert every in-surface `.py` file `compile()`s cleanly (the
    FUNCTION gate's guard-layer sibling -- a leak-check answers "does the private name
    still appear", never "did rewriting it produce valid source"; this closes that gap on
    published output bytes, § `check_python_syntax_valid`). The scan stays whole-destination
    always; an optional caller-supplied `written_paths` (§ chunk C3, docs/plans/
    2026-08-14-percolate-round-deadlock-and-gate-attribution.md D2) only changes how a
    FAILURE is worded -- written-this-round vs pre-existing-in-destination -- never what
    gets scanned.
  - `extensionless-shebang-absent`: assert zero EXTENSIONLESS files with a `#!` first line
    exist under a caller-supplied set of TOP-LEVEL subtrees (default `bin`/`hooks`/`lib`) --
    ports the bash `10-assert-inert-doctrine-only.sh` AC2b block (an extensionless
    executing-script leak that AC2a's extension-glob check cannot see, since it has no
    extension to match). Scoped strictly to the named top-level subtrees, not a whole-tree
    walk -- a nested extensionless-shebang file elsewhere in the surface (e.g. under a
    skill's own `src/lib/`) is out of scope for this check by design (§ docs/plans/
    2026-07-24-extirpate-orphaned-claude-central-publish-shell.md § Amendment 2026-07-24,
    chunk C6).
  - `bin-script-self-name-matches-basename`: assert a published `coordinator/bin/*.py`
    script's own docstring-header self-name literal (the module docstring's opening
    `<name>.py — ...` line) agrees with its actual published basename -- the durable half
    of state/bug-backlog/2026-08-26-two-depersonalize-rows-disagree-about-on-6206b36eac92.
    yaml (§ `check_bin_script_self_name_matches_basename` for scope and anti-scope: this is
    NOT a general path-literal-resolves guard).

Severity (§ `guardEntry.severity`, additive field, default `'error'`): a guard entry may
mark itself `'info'` to make it advisory-only. `run_guard` always RUNS the underlying
check for an `'info'` guard (so its finding is real, not skipped) but forces the returned
`GuardResult.ok` to `True` regardless of what the check computed, preserving the check's
`message`. This is deliberately fail-safe AT THIS BOUNDARY, not by convention at every
consumer: the engine never computes `all(r.ok)` itself (§ below) -- gating lives in
whichever consumer reads the wire -- and claude-klabauter cannot force every such consumer to
special-case `severity` before folding a result into its own pass/fail. Forcing `ok=True`
here means a naive `all(r.ok for r in results)` consumer is safe by construction; an
`'info'` guard can never flip its gate. A consumer that wants the advisory reads
`severity`/`message` directly.

`severity` governs ONLY the returned `ok`/pass-fail contribution -- it says nothing about
whether the underlying check kind performs a side effect, because most check kinds are
pure assertions with no side effect to suppress. For the ONE kind that mutates the target
tree as its check action (`prune-stale-paths`, § `_SIDE_EFFECTING_KINDS`), `severity:
'info'` is REJECTED at dispatch time (`GuardCheckError`) rather than silently accepted:
the check always deletes before `run_guard` ever looks at `severity`, so `'info'` on a
side-effecting kind can only ever mean the store author believes they are disabling the
deletion, which they are not -- an "advisory delete" is incoherent by design, not merely
underdocumented.

Runtime-sourced pattern (`no-residual-pattern` only, § `params.pattern_source`): a guard
entry may supply `params['pattern_source']` (an opaque descriptor -- a dict/str this
module never interprets) INSTEAD OF a literal `params['pattern']`. `run_guard` resolves
`pattern_source` into a concrete `pattern` via an INJECTED `pattern_resolver` callable
supplied by the caller -- this module stays content-free (it does not import or query any
registry; the resolver's shape and any registry lookup it performs belong entirely to the
ops layer that constructs it). Supplying `pattern_source` with no `pattern_resolver`
threaded in is a loud `GuardCheckError`, never a silent fallback to a static/empty
pattern; supplying BOTH `pattern` and `pattern_source` on the same entry is ambiguous and
also raises.

One discovery routine, no triplication (§ Anti-scope): every check that needs to walk the
target tree does so via `coordinator_core.percolate.surface.iter_surface_files` -- there
is no second hand-authored plugin-alternation / exclude-prefix walk in this module. The
shell's failure mode this closes is three independently hand-synced copies of that walk
drifting apart across guard call-sites; here there is exactly one.

Rename-manifest reconciliation (v3 seam -- § Transform-kind taxonomy `basename-rename`
row; chunk C11 v3-amend c8366f2e): a guard operating on an ENUMERATED file-list captured
before a `basename-rename` pass ran is stale -- the old path may no longer exist on disk,
producing a false-ENOENT that silently drops the (now-renamed) file from coverage instead
of loudly failing. Every check function in this module that accepts a pre-captured
file-list also accepts an optional `rename_manifest` (a
`coordinator_core.percolate.rewrite_basename.RenameManifest`); when supplied, each
enumerated path is resolved through `rename_manifest.resolve(path)` before any existence
check, so a guard reconciles a stale pre-rename list against the renamed files rather than
silently skipping them.

Override-env (§ Store schema `guardEntry.override_env`): a check spec may name an
environment variable that, when set (to any non-empty value), bypasses that specific
check entirely. The override-env NAME is store data -- this module never hardcodes which
variable name bypasses which check; it only implements the generic "is this override env
var set" test.

Negative-spec: this module contains zero repo-specific scrub content -- no persona names,
no codenames, no DoE path literals, and no hardcoded consumer-home literal. Every path,
pattern, and override-env name a check operates on is a runtime argument sourced from a
`guardEntry` in store data; this module supplies only the generic check mechanics and the
single discovery routine dependency.

Spec backlink: pln-percolation-engine-for-claude-klabauter--30c51f § Transform-kind
taxonomy (`guard` row, A13/A17-A20), § Store schema (`guardEntry`), chunk C11
(v3-amend c8366f2e: rename-manifest seam; v3-amend 652add0c: file-count-delta
effective-count note; v3-amend ca592759: leak-check per-target hook-set member).
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from coordinator_core.percolate.prune_dead_registrations import (
    _OPS_INIT_RELATIVE_PARTS,
    _REGISTRY_MAP_RELATIVE_PARTS,
    find_eager_op_modules_assign,
    find_op_module_map_assign,
    resolve_module_file,
    resolve_ops_registry,
    resolve_registry_map,
    strip_payload_prefix,
)
from coordinator_core.percolate import guard_cache
from coordinator_core.percolate.protect import mask_protected_literals
from coordinator_core.percolate.rewrite_basename import RenameManifest
from coordinator_core.percolate.surface import STRUCTURAL_NEVER_PUBLISHED_PREFIXES, iter_surface_files
from coordinator_core.wire_paths import rel_id

# `.git`/`.fleet-env` are destination-repo plumbing and a dest-local virtualenv,
# never publish-owned content -- the SAME structural fact
# `surface.STRUCTURAL_NEVER_PUBLISHED_PREFIXES` names as the single shared source of
# truth (see that constant's own docstring for the full worked rationale: why
# STRUCTURAL, not a ratified content exception, and why scoped by exact name rather
# than a broad rule or a `.gitignore` read). `engine._STRUCTURAL_NEVER_PUBLISHED_
# PREFIX`, `rewrite_basename`'s constant of the same name, and
# `coordinator/bin/publish.py`'s own name are the other enforcement points reading
# the same structural fact.
#
# Applied to the shared guard walk (§ `_walk_for_guard`, which states the per-kind
# consequences) rather than to one check, because every guard kind that walks the tree
# asks a question about PUBLISHED content, and no guard kind's question is ever about
# `.git` or a dest-local venv.
_STRUCTURAL_NEVER_PUBLISHED_PREFIXES = STRUCTURAL_NEVER_PUBLISHED_PREFIXES


@dataclass(frozen=True)
class GuardResult:
    """Outcome of running one guard/assert check spec.

    `ok` is True if the check passed (or was bypassed via override-env). `message`
    carries a human-readable explanation -- populated on failure, and on a bypass (so a
    passing-via-override run is still distinguishable from a genuine pass in a report).

    `severity` is `'error'` (default) or `'info'`. An `'info'` result always carries
    `ok=True` regardless of the underlying check's actual finding (§ module docstring,
    Severity) -- the finding itself is preserved in `message`.
    """

    kind: str
    ok: bool
    message: str = ''
    bypassed: bool = False
    severity: str = 'error'
    skipped: bool = False


class GuardCheckError(ValueError):
    """Raised when a guard entry's `params` are missing a field the check kind requires."""


def _is_overridden(override_env: str | None) -> bool:
    """Return True if `override_env` names an environment variable that is set (non-empty).

    The env var NAME is store data (§ module docstring) -- this function only implements
    the generic bypass test, never a hardcoded variable name.
    """
    if not override_env:
        return False
    return bool(os.environ.get(override_env))


def _resolve_paths(paths: list[str], rename_manifest: RenameManifest | None) -> list[str]:
    """Reconcile a possibly-stale enumerated path list against a rename manifest.

    Applies `rename_manifest.resolve()` to every entry unconditionally (a no-op for any
    path the manifest's renames did not touch) -- see module docstring, rename-manifest
    reconciliation. With no manifest supplied, returns `paths` unchanged.
    """
    if rename_manifest is None:
        return list(paths)
    return [rename_manifest.resolve(path) for path in paths]


def check_source_dir_absent(target_root: str | Path, params: dict) -> GuardResult:
    """Assert a source-tree directory does NOT exist under `target_root`.

    `params['path']` is a path relative to `target_root`. Passes if the path is absent;
    fails with a message naming the still-present path otherwise.
    """
    if 'path' not in params:
        raise GuardCheckError("source-dir-absent requires params['path']")
    candidate = Path(target_root) / params['path']
    if candidate.exists():
        return GuardResult(kind='source-dir-absent', ok=False, message=f'{params["path"]} still present')
    return GuardResult(kind='source-dir-absent', ok=True)


def check_prune_stale_paths(target_root: str | Path, params: dict) -> GuardResult:
    """Delete each of `params['paths']` (relative to `target_root`) if present, then pass.

    An active corrective action, not a pure assertion -- mirrors the shell's stale-path
    prune sweep (a directory is removed with `shutil.rmtree`, a file with `Path.unlink`).
    Always reports `ok=True` on completion (there is nothing left to fail on once the
    prune has run); a path that was never present is simply skipped.
    """
    if 'paths' not in params:
        raise GuardCheckError("prune-stale-paths requires params['paths']")
    root = Path(target_root)
    for rel in params['paths']:
        candidate = root / rel
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        elif candidate.exists() or candidate.is_symlink():
            candidate.unlink()
    return GuardResult(kind='prune-stale-paths', ok=True)


def check_dirs_absent(target_root: str | Path, params: dict) -> GuardResult:
    """Assert every path in `params['paths']` (relative to `target_root`) is absent."""
    if 'paths' not in params:
        raise GuardCheckError("dirs-absent requires params['paths']")
    root = Path(target_root)
    present = [rel for rel in params['paths'] if (root / rel).exists()]
    if present:
        return GuardResult(kind='dirs-absent', ok=False, message=f'still present: {present}')
    return GuardResult(kind='dirs-absent', ok=True)


def check_dirs_present(target_root: str | Path, params: dict) -> GuardResult:
    """Assert every path in `params['paths']` (relative to `target_root`) is present."""
    if 'paths' not in params:
        raise GuardCheckError("dirs-present requires params['paths']")
    root = Path(target_root)
    missing = [rel for rel in params['paths'] if not (root / rel).exists()]
    if missing:
        return GuardResult(kind='dirs-present', ok=False, message=f'missing: {missing}')
    return GuardResult(kind='dirs-present', ok=True)


def check_forbidden_script_globs(target_root: str | Path, params: dict) -> GuardResult:
    """Assert no file under `target_root` matches a caller-supplied forbidden glob.

    `params['globs']` are basename glob patterns (e.g. `'*.local.sh'`). Walks the target
    via the single discovery routine (`iter_surface_files`) -- `params` must supply at
    least one of `include_extensions`/`shebang_prefixes` (optionally narrowed further by
    `exclude_prefixes`/`exclude_basenames`/`exclude_globs`) to scope the walk to the
    file-surface the rest of the run uses; supplying neither raises `GuardCheckError`
    (§ `_walk_for_guard`) rather than silently scanning zero files -- callers that want
    the exact percolation surface should pass the store's own file-surface params here.
    """
    if 'globs' not in params:
        raise GuardCheckError("forbidden-script-globs requires params['globs']")
    root = Path(target_root)
    globs = params['globs']

    import fnmatch

    hits = []
    for path in _walk_for_guard(root, params):
        name = path.name
        if any(fnmatch.fnmatch(name, pattern) for pattern in globs):
            hits.append(rel_id(path, root))

    if hits:
        return GuardResult(kind='forbidden-script-globs', ok=False, message=f'forbidden files present: {hits}')
    return GuardResult(kind='forbidden-script-globs', ok=True)


#: Diagnosability caps for `check_no_residual_pattern`'s content-hit detail (§ its own
#: docstring). Bounds a single publish-log emission to a fixed worst-case size regardless
#: of how many times the pattern matches inside one runaway file, or how many files
#: match -- a silent cap is its own diagnosability failure, so both caps are always named
#: in the message when they truncate (never a quiet drop).
_RESIDUAL_PATTERN_MAX_HITS_PER_FILE = 5
_RESIDUAL_PATTERN_MAX_TOTAL_DETAIL_HITS = 20
_RESIDUAL_PATTERN_EXCERPT_MAX_CHARS = 160
#: Separate, explicit cap on the "N more hit(s) not shown" per-file truncation-notice
#: LINES themselves (§ `check_no_residual_pattern` docstring's "cannot dump unbounded
#: text" guarantee) -- those notice lines are not detail hits and were not counted
#: against `_RESIDUAL_PATTERN_MAX_TOTAL_DETAIL_HITS`, so a leak spanning many files
#: (each contributing its own notice line once the detail-hit budget is exhausted)
#: could otherwise grow the message by one line per file with no bound. Once this
#: cap is reached, further per-file notices are folded into ONE final aggregate line
#: naming the true remaining-file and remaining-hit totals, never silently dropped.
_RESIDUAL_PATTERN_MAX_TRUNCATION_NOTICE_LINES = 5


def _residual_pattern_content_detail(
    content: str, pattern: 're.Pattern[str]', per_file_cap: int
) -> tuple[list[tuple[int, str, str]], int]:
    """Return up to `per_file_cap` (line_no, matched_text, line_excerpt) hits plus the
    file's TRUE total match count (so a truncated file can say "N more, M total").
    """
    all_matches = list(pattern.finditer(content))
    hits: list[tuple[int, str, str]] = []
    for m in all_matches[:per_file_cap]:
        line_no = content.count('\n', 0, m.start()) + 1
        line_start = content.rfind('\n', 0, m.start()) + 1
        line_end = content.find('\n', m.end())
        if line_end == -1:
            line_end = len(content)
        excerpt = content[line_start:line_end].strip()
        if len(excerpt) > _RESIDUAL_PATTERN_EXCERPT_MAX_CHARS:
            excerpt = excerpt[:_RESIDUAL_PATTERN_EXCERPT_MAX_CHARS] + '...'
        hits.append((line_no, m.group(0), excerpt))
    return hits, len(all_matches)


def check_no_residual_pattern(
    target_root: str | Path,
    params: dict,
    *,
    protected_literals: list[str] | tuple[str, ...] | None = None,
) -> GuardResult:
    """Assert no in-surface file's content matches `params['pattern']` (a leak-check).

    A PER-TARGET composable hook-set member (§ module docstring) -- callers decide
    per-target whether this check runs by including `'guard'` (and this entry) in that
    target's composed hook-set; this function itself has no notion of "global". Walks
    the target via the single discovery routine, scoped by the same optional
    file-surface params as `forbidden-script-globs`.

    `params['pattern']` may instead be supplied as `params['pattern_source']` (§ module
    docstring, runtime-sourced pattern) -- an opaque descriptor this function never sees
    directly, because `run_guard` resolves `pattern_source` into a concrete `pattern` via
    an injected `pattern_resolver` BEFORE dispatching here. This function itself only ever
    receives an already-resolved `params['pattern']` string; it has no notion of
    `pattern_source` at all.

    Diagnosability (this is a leak-CHECK, not a leak-prevention gate -- the payload
    already sits on disk in the developer's own tree by the time this runs): a content
    hit's message names the file, the matched TEXT, the line number, and a bounded
    excerpt of that line, so a reader is never forced to re-derive the guard's own
    finding by hand (the incident this closes: a consumer repo could not tell "his"
    the gendered-pronoun backstop matched from a codename leak by inspection alone).
    The matched text and excerpt are echoed UNREDACTED -- deliberately, not by
    default: the string is already present, unencrypted, in a file the developer
    running this local tool already owns and can `cat` themselves, so masking it in
    the guard's own output would only relocate the same bytes one command away while
    making the failure harder to act on. Detail is capped per-file and in total
    (`_RESIDUAL_PATTERN_MAX_HITS_PER_FILE` / `_RESIDUAL_PATTERN_MAX_TOTAL_DETAIL_HITS`),
    and the per-file "N more hit(s) not shown" truncation-NOTICE lines are themselves
    capped too (`_RESIDUAL_PATTERN_MAX_TRUNCATION_NOTICE_LINES`), so neither one
    runaway file NOR a leak spanning many truncated files can dump unbounded text into
    a publish log; truncation is always stated alongside the true total, never silent
    -- notices past that cap fold into one final aggregate line.

    `params['line_allow_marker']`, when set, exempts every content line carrying that marker
    text (each exemption is one marked line; path hits are never exempt).

    `protected_literals` (§ `protect.py`), if supplied, is masked out of both the rel-path
    and the content BEFORE this pattern search runs -- a store-declared functional literal
    (e.g. `example_retrieval_repo` in `repo = "project_rag"`) that happens to contain a fleet codename
    must not fail this leak-check, since the guard's job is finding an accidental codename
    mention, not this row's own deliberately-shipped wire value. Any OTHER occurrence of the
    same codename text outside a protected literal still matches and still fails.
    """
    if 'pattern' not in params:
        raise GuardCheckError("no-residual-pattern requires params['pattern']")
    root = Path(target_root)
    pattern_text = params['pattern']
    pattern = re.compile(pattern_text)
    line_allow_marker = params.get('line_allow_marker') or ''
    clean_memo = guard_cache.clean_memo(
        'no-residual-pattern',
        repr((pattern_text, line_allow_marker, tuple(repr(e) for e in protected_literals or ()))),
    )
    # The memo is keyed on file bytes alone; a path-scoped literal makes the verdict
    # depend on the path too, so identical bytes at an excluded path must not reuse it.
    path_scoped = any(not isinstance(e, str) for e in protected_literals or ())

    path_hits: list[str] = []
    content_hits: list[str] = []
    content_detail: list[str] = []
    detail_hits_used = 0
    detail_truncated_files = 0
    truncation_notice_lines_used = 0
    unnoticed_truncated_files = 0
    unnoticed_truncated_hits = 0
    skipped: list[str] = []
    for path in _walk_for_guard(root, params):
        rel = rel_id(path, root)
        # Path-component check first, and unconditionally -- this does NOT require
        # reading the file, so it must run (and be able to fail the gate on its own)
        # even for a file the content read below cannot access. A leaking directory
        # segment or basename on an unreadable file must not be swallowed by the
        # skip/Tier-2 branch further down.
        masked_rel, _ = mask_protected_literals(rel, protected_literals, rel)
        if pattern.search(masked_rel):
            path_hits.append(rel)
        try:
            data = guard_cache.read_bytes(path)
        except OSError as exc:
            skipped.append(rel)
            print(
                f'no-residual-pattern: WARNING: could not read {rel} ({exc}) -- '
                f'this file was NOT scanned for the leak pattern',
                file=sys.stderr,
            )
            continue
        if not path_scoped and clean_memo.known_clean(data):
            continue
        content = guard_cache.decode_text(data, errors='ignore')
        masked_content, _ = mask_protected_literals(content, protected_literals, rel)
        if line_allow_marker:
            masked_content = '\n'.join(
                '' if line_allow_marker in line else line for line in masked_content.split('\n')
            )
        if not pattern.search(masked_content):
            if not path_scoped:
                clean_memo.record_clean(data)
        else:
            content_hits.append(rel)
            remaining_budget = _RESIDUAL_PATTERN_MAX_TOTAL_DETAIL_HITS - detail_hits_used
            per_file_cap = max(0, min(_RESIDUAL_PATTERN_MAX_HITS_PER_FILE, remaining_budget))
            file_hits, file_total = _residual_pattern_content_detail(masked_content, pattern, per_file_cap)
            for line_no, matched_text, excerpt in file_hits:
                content_detail.append(f'{rel}:{line_no}: matched {matched_text!r} in "{excerpt}"')
            detail_hits_used += len(file_hits)
            if file_total > len(file_hits):
                detail_truncated_files += 1
                unshown_in_file = file_total - len(file_hits)
                if truncation_notice_lines_used < _RESIDUAL_PATTERN_MAX_TRUNCATION_NOTICE_LINES:
                    truncation_notice_lines_used += 1
                    content_detail.append(
                        f'{rel}: {unshown_in_file} more hit(s) not shown '
                        f'({file_total} total in this file)'
                    )
                else:
                    # § `_RESIDUAL_PATTERN_MAX_TRUNCATION_NOTICE_LINES` -- notice-line
                    # budget itself exhausted; fold into the one aggregate line appended
                    # after the walk, never emit an unbounded per-file line here.
                    unnoticed_truncated_files += 1
                    unnoticed_truncated_hits += unshown_in_file

    if unnoticed_truncated_files:
        content_detail.append(
            f'and {unnoticed_truncated_files} further file(s) had unshown hits '
            f'({unnoticed_truncated_hits} total) beyond the truncation-notice cap '
            f'({_RESIDUAL_PATTERN_MAX_TRUNCATION_NOTICE_LINES} notice lines max)'
        )

    if path_hits or content_hits:
        # Distinguish the two failure classes in the message -- a maintainer seeing
        # this red needs to know instantly whether to scrub file text (content) or
        # rename a path (path component), since those are completely different
        # fixes. Both hit sets fail the gate; neither masks the other.
        parts = []
        if path_hits:
            parts.append(f'residual pattern found in path: {path_hits}')
        if content_hits:
            parts.append(f'residual pattern found in: {content_hits}')
        if content_detail:
            parts.append(f'pattern {pattern_text!r} matched -- ' + '; '.join(content_detail))
        return GuardResult(kind='no-residual-pattern', ok=False, message='; '.join(parts))

    # --- Tier 2 (behaviour change -- PM sign-off required) ---------------------
    # A file this gate could not read was never scanned for the leak pattern.
    # "Could not check" is not "is clean" -- especially for a gate whose entire
    # job is deciding whether it is safe to publish. Treat any skip as a gate
    # FAILURE rather than silently returning ok=True on incomplete coverage.
    if skipped:
        return GuardResult(
            kind='no-residual-pattern',
            ok=False,
            message=(
                f'{len(skipped)} file(s) could not be read and were NOT scanned '
                f'for the residual pattern (treating as a leak-check failure, '
                f'not a pass): {skipped}'
            ),
        )
    # --- end Tier 2 --------------------------------------------------------------

    return GuardResult(kind='no-residual-pattern', ok=True)


def check_file_count_delta(
    target_root: str | Path,
    params: dict,
    *,
    effective_source_count: int | None = None,
) -> GuardResult:
    """Assert the target's file count matches the EFFECTIVE source count within tolerance.

    `effective_source_count` MUST be computed by the caller against the file-surface
    (i.e. AFTER file-surface.py's include/exclude resolution has netted out
    transform-exempt/ignore-listed source files) -- never a raw hand-count of the source
    tree (§ chunk C11 v3-amend 652add0c). If the caller does not supply
    `effective_source_count`, it falls back to `params['expected_count']` (a store-declared
    fixed expectation) -- one of the two MUST be provided.

    `params['tolerance']` (default 0) is the maximum allowed absolute delta between the
    target's actual file count and the effective expected count.

    Zero-observation is never a silent PASS. An observed count of 0 that lands inside
    tolerance (expected 0, or expected within tolerance of 0) is indistinguishable from a
    guard whose scoping params match nothing -- the class of defect where a store row
    "passes" every run without ever having counted a file. Such a run FAILS unless the
    entry opts in with `params['allow_empty']: true`, which is the only way to say "an
    empty surface is the expected outcome here" (§ negative-spec: `_walk_for_guard`'s
    fail-loud covers the params that are structurally unable to match; this covers params
    that CAN match but did not).
    """
    root = Path(target_root)
    expected = effective_source_count if effective_source_count is not None else params.get('expected_count')
    if expected is None:
        raise GuardCheckError(
            "file-count-delta requires either effective_source_count or params['expected_count']"
        )
    tolerance = params.get('tolerance', 0)

    actual = sum(1 for path in _walk_for_guard(root, params))
    delta = abs(actual - expected)
    if actual == 0 and delta <= tolerance and not params.get('allow_empty'):
        return GuardResult(
            kind='file-count-delta',
            ok=False,
            message=(
                f'observed 0 files under {root} (expected {expected} +/-{tolerance}), so this '
                f"check counted nothing and cannot attest to anything. Either the guard entry's "
                f'file-surface params select no file in this tree, or the tree is empty. Set '
                f"params['allow_empty'] to true if an empty surface really is the expected "
                f'outcome for this row.'
            ),
        )
    if delta > tolerance:
        return GuardResult(
            kind='file-count-delta',
            ok=False,
            message=f'expected {expected} (+/-{tolerance}), got {actual} (delta {delta})',
        )
    return GuardResult(kind='file-count-delta', ok=True)


def check_canary_survival(target_root: str | Path, params: dict) -> GuardResult:
    """Assert a canary path's content still contains a caller-supplied marker.

    `params['path']` is relative to `target_root`; `params['marker']` is the exact
    substring expected to still be present in the canary file's content, unmodified by
    the pipeline. Fails if the canary is missing entirely or the marker is absent.
    """
    if 'path' not in params or 'marker' not in params:
        raise GuardCheckError("canary-survival requires params['path'] and params['marker']")
    candidate = Path(target_root) / params['path']
    if not candidate.exists():
        return GuardResult(kind='canary-survival', ok=False, message=f'canary {params["path"]} missing')
    try:
        content = candidate.read_text(encoding='utf-8', errors='ignore')
    except OSError as exc:
        return GuardResult(kind='canary-survival', ok=False, message=f'canary {params["path"]} unreadable: {exc}')
    if params['marker'] not in content:
        return GuardResult(kind='canary-survival', ok=False, message=f'canary {params["path"]} missing marker')
    return GuardResult(kind='canary-survival', ok=True)


def check_source_glob_present(source_root: str | Path, params: dict) -> GuardResult:
    """Assert the SOURCE tree has >=1 file matching `params['glob']` (presence, not count).

    `params['glob']` is a basename glob (e.g. `'*.py'`), required -- raises
    `GuardCheckError` if absent, mirroring the other checks' required-param pattern.
    `params['subdir']`, if supplied, restricts the walk to `source_root / subdir` rather
    than the whole source tree. Walks via the single discovery routine (`_walk_for_guard`
    / `iter_surface_files`) plus `fnmatch` on `path.name`, exactly like
    `check_forbidden_script_globs` -- no second hand-rolled walk (§ module docstring, one
    discovery routine). `params` must supply at least one of `include_extensions`/
    `shebang_prefixes`; supplying neither raises `GuardCheckError` (§ `_walk_for_guard`)
    rather than silently scanning zero files. Passes if >=1 file matches; fails with a
    message naming the glob and the searched root otherwise.
    """
    if 'glob' not in params:
        raise GuardCheckError("source-glob-present requires params['glob']")
    glob = params['glob']
    root = Path(source_root)
    search_root = root / params['subdir'] if params.get('subdir') else root

    import fnmatch

    for path in _walk_for_guard(search_root, params):
        if fnmatch.fnmatch(path.name, glob):
            return GuardResult(kind='source-glob-present', ok=True)

    return GuardResult(
        kind='source-glob-present',
        ok=False,
        message=f'no file matching {glob!r} found under {search_root}',
    )


def check_extensionless_shebang_absent(target_root: str | Path, params: dict) -> GuardResult:
    """Assert no EXTENSIONLESS file with a `#!` first line exists under `params['paths']`.

    `params['paths']` names TOP-LEVEL subtrees relative to `target_root` (default
    `['bin', 'hooks', 'lib']` if omitted, matching the bash oracle's own scope) -- a
    subtree that doesn't exist under `target_root` is silently skipped, not a failure
    (mirrors the `.sh`'s `[[ -d "$_d" ]] || continue`). Ports AC2b of the bash guard
    `10-assert-inert-doctrine-only.sh` (§ module docstring): AC2a's `forbidden-script-globs`
    catches extension-glob scripts; this check catches the extensionless remainder via the
    single discovery routine (`_walk_for_guard` / `iter_surface_files`), which already
    yields exactly the extensionless-shebang subset of a walk when `shebang_prefixes` is
    supplied with no `include_extensions` (`iter_surface_files` -> `is_in_surface`: a file
    is only yielded via the shebang path when `path.suffix == ''`, so every hit here is
    extensionless by construction -- no extra filter needed).

    Deliberately scoped to each named subtree independently (one `_walk_for_guard` call per
    `rel` in `params['paths']`) rather than a single whole-target walk -- a nested
    extensionless-shebang file living OUTSIDE these top-level subtrees (e.g. under a
    skill's own `src/lib/`) must survive this check (§ AC4), so this check never walks
    `target_root` itself.
    """
    paths = params.get('paths') or ['bin', 'hooks', 'lib']
    root = Path(target_root)
    hits: list[str] = []
    for rel in paths:
        subtree = root / rel
        if not subtree.exists():
            continue
        for path in _walk_for_guard(subtree, {'shebang_prefixes': ['#!']}):
            hits.append(rel_id(path, root))

    if hits:
        return GuardResult(
            kind='extensionless-shebang-absent',
            ok=False,
            message=f'extensionless shebang script(s) present: {hits}',
        )
    return GuardResult(kind='extensionless-shebang-absent', ok=True)


def check_python_syntax_valid(
    target_root: str | Path,
    params: dict,
    *,
    written_paths: frozenset[str] | None = None,
) -> GuardResult:
    """Assert every in-surface `.py` file under `target_root` parses cleanly (`ast.parse`).

    The FUNCTION-level sibling of `no-residual-pattern`'s leak check: a leak-check answers
    "does the private name still appear in the payload", never "did rewriting it produce
    valid source" -- a store rule that consumes a suffix (e.g. an assignment-target
    identifier corrupted into an expression, `\\bstem\\b` glue-matching into a bare
    local variable) ships unparseable Python while every leak-check stays green, since the
    corrupted spelling contains no residual private token at all. This guard closes that
    gap at the one point that can observe it: published OUTPUT bytes, not vocabulary
    declarations (store-load-time validation cannot see this class of defect -- a
    hand-authored rule is well-formed as a declaration; the damage depends entirely on
    what identifier shape it lands on in the tree).
  - `registration-liveness`: defence-in-depth over
    `coordinator_core.percolate.prune_dead_registrations` (§ chunk C1 of
    docs/plans/2026-08-10-the-payload-derives-its-own-op-registrations.md): C1's transform
    makes the payload's `_EAGER_OP_MODULES` correct at percolation time; this guard
    asserts it STAYED correct. Parses `<target_root>/coordinator_core/ops/__init__.py` and
    resolves every `_EAGER_OP_MODULES` row's module path against `target_root` using the
    SAME resolver C1 uses (`resolve_module_file`, imported -- never reimplemented, per
    that module's own "shared seam" docstring, since a divergent second resolver is
    exactly how this defect class returns). Fails naming every unresolvable row. See
    `check_registration_liveness` for the absent-`ops/__init__.py` semantics.

    `params['include_extensions']` is REQUIRED (like every other `_walk_for_guard` caller)
    so a store entry that supplies neither `include_extensions` nor `shebang_prefixes`
    fails loud rather than silently walking zero files; callers scan for `.py` specifically
    by passing `include_extensions: ["*.py"]` (this check itself additionally filters to
    `path.suffix == '.py'`, so a broader include-set such as the row's full file-surface
    passed here does not silently widen what actually gets parsed).

    A file that cannot be read is reported as a failure (§ `no-residual-pattern`'s own
    Tier-2 "could not check is not is clean" precedent) -- never silently skipped, since a
    syntax gate whose entire job is deciding whether the payload can start must not treat
    an unreadable file as an implicit pass.

    `written_paths`, if supplied by the caller, is the set of rel-ids this round actually
    wrote (§ chunk C3, docs/plans/2026-08-14-percolate-round-deadlock-and-gate-attribution.md
    D2). The scan itself stays whole-destination regardless -- asserting the destination is
    valid is this guard's job, and narrowing the walk to the written set would just trade
    one blind spot for another. What `written_paths` changes is the FAILURE MESSAGE only:
    each failing rel-id is split into "written by this round" vs "pre-existing in the
    destination" so a reader isn't told a stale file a week old is "in the written payload."
    This module never derives that set itself (e.g. via mtime) -- mtime is not a sound
    provenance signal, so an unsupplied `written_paths` degrades to today's undifferentiated
    wording rather than guessing.

    Caveat (Review: code-reviewer P4): `written_paths` actually tracks "files the
    content-transform sweep successfully visited this round" (`run_content_transform_
    sweep`'s `visited_sink`), not "files whose bytes changed" -- a visited-but-unmutated
    file is still "written by this round" in the failure message below, since it was
    still part of this round's rsync'd payload. That is the correct signal for this
    guard's purpose (this-round payload vs stale destination leftover); the message
    wording is intentionally not changed to match.
    """
    root = Path(target_root)
    # Track (rel, message) pairs rather than a single
    # 'rel: detail' string so the written-vs-preexisting split below compares the
    # rel-id we already resolved via `rel_id()`, never a re-split of the rendered
    # message. A rel-id containing a literal ':' (valid on POSIX) would otherwise
    # get truncated by a `.split(':', 1)[0]` re-parse and mis-bucket that failure's
    # attribution even though `ok=False` itself is unaffected either way.
    failures: list[tuple[str, str]] = []
    clean_memo = guard_cache.clean_memo('python-syntax-valid', sys.version)
    for path in _walk_for_guard(root, params):
        if path.suffix != '.py':
            continue
        rel = rel_id(path, root)
        try:
            data = guard_cache.read_bytes(path)
            if clean_memo.known_clean(data):
                continue
            source = guard_cache.decode_text(data, errors='strict')
        except (UnicodeDecodeError, OSError) as exc:
            failures.append((rel, f'{rel}: unreadable ({exc})'))
            continue
        try:
            compile(source, str(path), 'exec', dont_inherit=True)
        except SyntaxError as exc:
            failures.append((rel, f'{rel}: {exc.__class__.__name__}: {exc}'))
        else:
            clean_memo.record_clean(data)

    if failures:
        rendered = [message for _rel, message in failures]
        if written_paths is None:
            message = f'{len(failures)} published .py file(s) failed to parse: {rendered}'
        else:
            written_failures = [message for rel, message in failures if rel in written_paths]
            preexisting_failures = [message for rel, message in failures if rel not in written_paths]
            parts = []
            if written_failures:
                parts.append(f'{len(written_failures)} written by this round: {written_failures}')
            if preexisting_failures:
                parts.append(f'{len(preexisting_failures)} pre-existing in the destination: {preexisting_failures}')
            message = f'{len(failures)} published .py file(s) failed to parse -- ' + '; '.join(parts)
        return GuardResult(
            kind='python-syntax-valid',
            ok=False,
            message=message,
        )
    return GuardResult(kind='python-syntax-valid', ok=True)


def check_registration_liveness(target_root: str | Path, params: dict) -> GuardResult:
    """Assert every `_EAGER_OP_MODULES` row in the payload resolves to a real file.

    Defence-in-depth over `prune_dead_registrations.prune_dead_registrations` (§ module
    docstring): that transform makes the payload's `_EAGER_OP_MODULES` correct at
    percolation time; this guard asserts it stayed correct. Locates `ops/__init__.py`
    under `target_root` via `prune_dead_registrations.resolve_ops_registry` -- probing
    both shapes `target_root` can be in (contains `coordinator_core/`, or IS the
    `coordinator_core` payload itself, per `engine.py::_repo_relative_path`'s docstring)
    -- parses it with `ast`, and resolves every row's module path against `target_root`
    via `prune_dead_registrations.resolve_module_file` (through `strip_payload_prefix`
    for the 'is' shape) -- THE shared seam, imported rather than reimplemented (a
    divergent second resolver is exactly how this defect class returns). Fails naming
    every unresolvable module path, sorted for a stable message.

    Absent-file semantics (deliberate, not a pass-by-default): a `target_root` with no
    `ops/__init__.py` under EITHER shape FAILS this guard rather than silently passing.
    This guard exists specifically to prove the ops registry stayed live; a target that
    never shipped the file this guard exists to check is indistinguishable, from
    outside, between "this target legitimately carries no ops registry" and "the
    registry vanished and nobody noticed" -- the same "observed nothing is not observed
    clean" precedent `check_file_count_delta` establishes for a zero-file walk. A target
    that genuinely never ships an ops registry should not carry this guard entry at all,
    rather than relying on a silent pass here.
    """
    ops_init_path, shape = resolve_ops_registry(target_root)
    if ops_init_path is None:
        root = Path(target_root)
        probed = (
            # Imports the shared
            # constant `prune_dead_registrations._OPS_INIT_RELATIVE_PARTS`
            # rather than re-literalising the probe tuple here, so this error
            # message cannot drift out of sync with what
            # `resolve_ops_registry` actually probes (the exact "divergent
            # second resolver" pattern the module docstring names as the
            # failure class to avoid).
            root.joinpath(*_OPS_INIT_RELATIVE_PARTS),
            root.joinpath('ops', '__init__.py'),
        )
        return GuardResult(
            kind='registration-liveness',
            ok=False,
            message=(
                f'ops/__init__.py not found under {target_root} (probed {probed[0]} '
                f'and {probed[1]}) -- this guard cannot attest to _EAGER_OP_MODULES '
                f'liveness for a target that does not ship the ops registry. Remove '
                f'this guard entry if the target legitimately never ships an ops '
                f'registry.'
            ),
        )

    source = ops_init_path.read_text(encoding='utf-8')
    tree = ast.parse(source)

    assign_node = find_eager_op_modules_assign(tree)
    if assign_node is None:
        return GuardResult(
            kind='registration-liveness',
            ok=False,
            message=(
                f'_EAGER_OP_MODULES not found in {ops_init_path} -- this guard cannot '
                f'attest to liveness for a target whose ops registry declaration is '
                f'unlocatable (distinct from "declared with zero dead rows").'
            ),
        )
    list_node = assign_node.value
    if not isinstance(list_node, ast.List):
        return GuardResult(
            kind='registration-liveness',
            ok=False,
            message=(
                f'_EAGER_OP_MODULES in {ops_init_path} is not a list literal -- this '
                f'guard cannot attest to liveness.'
            ),
        )

    dead: list[str] = []
    for element in list_node.elts:
        if not isinstance(element, ast.Tuple) or not element.elts:
            continue
        module_path_node = element.elts[0]
        if not isinstance(module_path_node, ast.Constant) or not isinstance(module_path_node.value, str):
            continue
        module_path = module_path_node.value
        lookup_path = strip_payload_prefix(module_path, shape)
        if resolve_module_file(lookup_path, target_root) is None:
            dead.append(module_path)

    if dead:
        dead.sort()
        return GuardResult(
            kind='registration-liveness',
            ok=False,
            message=f'{len(dead)} dead _EAGER_OP_MODULES row(s) unresolvable under {target_root}: {dead}',
        )

    # Bug: state/bug-backlog/2026-08-26-the-published-mirror-advertises-5-ops-
    # it-c85f7d77431d.yaml -- `_EAGER_OP_MODULES` liveness (above) was already
    # covered, but `coordinator_core/ops/_registry_map.py::OP_MODULE_MAP` is a
    # SECOND registry naming the same modules and was never checked here, so
    # the exact asymmetry this guard exists to catch reopened through it. This
    # is the durable half of the fix: it fails the SAME way for the next
    # package excluded from the payload, not just the one that was actually
    # observed missing. Absent-`_registry_map.py` is NOT a failure here (unlike
    # the absent-`ops/__init__.py` case above) -- that file is itself a
    # performance-optimization seam (§ its own module docstring) some publish
    # targets may legitimately never ship, whereas every target carrying this
    # guard entry ships an ops registry by definition.
    registry_map_path, map_shape = resolve_registry_map(target_root)
    if registry_map_path is not None:
        map_source = registry_map_path.read_text(encoding='utf-8')
        map_tree = ast.parse(map_source)
        map_assign_node = find_op_module_map_assign(map_tree)
        if map_assign_node is not None and isinstance(map_assign_node.value, ast.Dict):
            dead_map_entries: list[str] = []
            for key_node, value_node in zip(map_assign_node.value.keys, map_assign_node.value.values):
                if key_node is None:
                    continue
                if not isinstance(value_node, ast.Constant) or not isinstance(value_node.value, str):
                    continue
                module_path = value_node.value
                lookup_path = strip_payload_prefix(module_path, map_shape)
                if resolve_module_file(lookup_path, target_root) is None:
                    dead_map_entries.append(module_path)
            if dead_map_entries:
                dead_map_entries.sort()
                return GuardResult(
                    kind='registration-liveness',
                    ok=False,
                    message=(
                        f'{len(dead_map_entries)} dead OP_MODULE_MAP row(s) unresolvable '
                        f'under {target_root}: {dead_map_entries}'
                    ),
                )
    return GuardResult(kind='registration-liveness', ok=True)


_COORDINATOR_CORE_IMPORT_PREFIX = 'coordinator_core.'


def _module_level_import_targets(tree: ast.Module):
    """Yield `(stmt, guarded)` for every `Import`/`ImportFrom` node reachable at
    MODULE IMPORT TIME -- top-level statements, and statements nested inside
    `If`/`Try`/`ClassDef`/`With` bodies (all of which execute the moment the
    module is imported) -- but NEVER inside a `FunctionDef`/`AsyncFunctionDef`
    body, since this repo uses deferred/lazy op imports deliberately (the
    lazy-ops backlog concern) and a lazy import cannot break at import time.

    `guarded` is True when the import sits inside a `try:` body whose
    `except` clause(s) include a bare `except:`, `except Exception`, or
    `except ImportError` (any `ast.Name`/`ast.Attribute`/`ast.Tuple` naming
    `ImportError` or `Exception`) -- an import the source itself already
    anticipates may fail is not a publish-time breakage.
    """
    results: list[tuple['ast.Import | ast.ImportFrom', bool]] = []

    def _handler_is_import_guard(handler: ast.ExceptHandler) -> bool:
        if handler.type is None:
            return True
        candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for node in candidates:
            name = node.id if isinstance(node, ast.Name) else getattr(node, 'attr', None)
            if name in ('ImportError', 'ModuleNotFoundError', 'Exception', 'BaseException'):
                return True
        return False

    def walk(stmts: list, guarded: bool) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                results.append((stmt, guarded))
            elif isinstance(stmt, ast.If):
                walk(stmt.body, guarded)
                walk(stmt.orelse, guarded)
            elif isinstance(stmt, ast.Try):
                body_guarded = guarded or any(_handler_is_import_guard(h) for h in stmt.handlers)
                walk(stmt.body, body_guarded)
                for handler in stmt.handlers:
                    walk(handler.body, guarded)
                walk(stmt.orelse, guarded)
                walk(stmt.finalbody, guarded)
            elif isinstance(stmt, ast.ClassDef):
                walk(stmt.body, guarded)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk(stmt.body, guarded)
            # `FunctionDef`/`AsyncFunctionDef` bodies deliberately not descended
            # into -- a lazy/deferred import does not break at module import time.

    walk(tree.body, False)
    return results


def _import_target_present(lookup_path: str, root: Path) -> bool:
    """A module file, a regular package, or an `__init__`-less namespace directory."""
    return resolve_module_file(lookup_path, root) is not None or root.joinpath(*lookup_path.split('.')).is_dir()


def check_published_import_resolves(target_root: str | Path, params: dict) -> GuardResult:
    """Assert every module-level `coordinator_core.*` import in a published `.py` file
    resolves to a file present under `target_root`.

    Sibling of `check_registration_liveness`, closing the gap that guard's own docstring
    names: `_EAGER_OP_MODULES`/`OP_MODULE_MAP` liveness covers the two DECLARED registries,
    but any OTHER published `.py` importing an unallowlisted `coordinator_core.*` module
    still breaks at import time on the mirror with nothing catching it at publish time.
    Reuses the SAME resolver seam `check_registration_liveness` uses -- `resolve_module_file`
    / `strip_payload_prefix`, imported from `prune_dead_registrations` rather than
    reimplemented (§ that module's own "shared seam" docstring) -- and the same shape
    probe (`resolve_ops_registry`) to decide whether `target_root` CONTAINS
    `coordinator_core/` or IS the `coordinator_core` payload itself.

    Each file is `ast.parse`d exactly once; every `Import`/`ImportFrom` target is read off
    that single tree (§ budget: no re-parsing per import, no process spawns). A file that
    fails to parse, or cannot be read, is silently skipped here -- `python-syntax-valid`
    already owns reporting that failure, and a file this check cannot parse has no imports
    this check can inspect either way.

    Scope is MODULE-IMPORT-TIME only (§ `_module_level_import_targets`): a lazy import
    inside a function body is out of scope by design (this repo's lazy-ops backlog
    concern), and a `try`/`except ImportError`-guarded import is exempted (the source
    already anticipates the failure, so it is not a publish-time breakage). A relative
    import (`from . import x`, `level > 0`) is skipped deliberately -- its `module`
    attribute is not a full dotted path, and resolving it correctly would require walking
    the importing file's own package location rather than the seam this guard reuses; a
    relative import can only ever resolve within the file's own already-published package
    tree, which is a different failure mode than the "some OTHER top-level module is
    missing from this payload" defect this guard targets.

    "Absent from the write set" (this `target_root`), never "absent from the mirror" --
    matches `check_registration_liveness`'s own semantics: a module that already exists on
    the mirror from a prior round but is not part of THIS payload's resolved tree is not a
    breakage this check can see or should report on.
    """
    root = Path(target_root)
    _, shape = resolve_ops_registry(target_root)

    failures: list[str] = []
    for path in _walk_for_guard(root, params):
        if path.suffix != '.py':
            continue
        try:
            source = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue

        rel = rel_id(path, root)
        for stmt, guarded in _module_level_import_targets(tree):
            if guarded:
                continue
            if isinstance(stmt, ast.ImportFrom):
                if stmt.level and stmt.level > 0:
                    continue
                module_path = stmt.module
            else:
                # `ast.Import` may name several dotted targets in one statement
                # (`import a, b.c`) -- check each independently.
                for alias in stmt.names:
                    module_path = alias.name
                    if not module_path or not module_path.startswith(_COORDINATOR_CORE_IMPORT_PREFIX):
                        continue
                    lookup_path = strip_payload_prefix(module_path, shape)
                    if not _import_target_present(lookup_path, root):
                        failures.append(f'{rel}: imports {module_path!r}, unresolvable under {target_root}')
                continue
            if not module_path or not module_path.startswith(_COORDINATOR_CORE_IMPORT_PREFIX):
                continue
            lookup_path = strip_payload_prefix(module_path, shape)
            if not _import_target_present(lookup_path, root):
                failures.append(f'{rel}: imports {module_path!r}, unresolvable under {target_root}')

    if failures:
        failures.sort()
        return GuardResult(
            kind='published-import-resolves',
            ok=False,
            message=f'{len(failures)} published import(s) unresolvable in the write set: {failures}',
        )
    return GuardResult(kind='published-import-resolves', ok=True)


_SELF_NAME_DOCSTRING_HEADER = re.compile(r'^"""([\w.-]+\.py)\s+—')


def check_bin_script_self_name_matches_basename(target_root: str | Path, params: dict) -> GuardResult:
    """Assert a published `coordinator/bin/*.py` script's own docstring-header self-name
    literal matches the file's actual published basename.

    Bug: state/bug-backlog/2026-08-26-two-depersonalize-rows-disagree-about-on-6206b36eac92.yaml
    -- two `base.depersonalize` rows both matched inside one source literal
    (a root-pointer generator script): the FILENAME rename took the shorter `claude-klabauter -> claude-
    klabauter` row, the CONTENT rewrite took a longer `claude-klabauter-live-root -> claude-klabauter-live-
    root` row, so the published file's OWN docstring header named a script the basename
    rename never produced. Fixed for that one collision by a precedence row (§
    `codename_provenance_seed.py`); this guard is the durable half -- it fails the SAME way
    for the next colliding pair, on PUBLISHED bytes, regardless of which two rows collide.

    Scope, deliberately narrow (anti-scope: this is NOT a general path-shaped-literal-must-
    resolve guard -- published prose is full of path-shaped strings that are not payload
    files: URLs, other repos' paths, example paths). This check only reads the ONE
    convention this bug's defect class actually depends on: a `coordinator/bin/*.py`
    script's module docstring opens with a line of the shape <name>.py, an em dash, then a
    description, naming itself (82 of the 420 scripts under `coordinator/bin/` follow this
    convention as of 2026-08-26, with zero pre-existing mismatches -- confirmed by scanning
    the source tree before landing this guard). A file with no such header, or whose header
    does not match this exact opening-line shape, is silently skipped -- this check has
    nothing to say about scripts that don't self-name in their own docstring, which is the
    majority.

    Scoped to `params['paths']` (default `['coordinator/bin']`), TOP-LEVEL `.py` files only
    (no recursion into `lib/` or other subdirectories) -- the exact surface the reported
    defect occurred on. A file under `target_root` that does not exist is silently skipped
    (mirrors `_walk_for_guard`'s `subtree.exists()` guard elsewhere in this module) rather
    than failing, since a target legitimately may not ship a `coordinator/bin/` at all.
    """
    root = Path(target_root)
    paths = params.get('paths') or ['coordinator/bin']
    mismatches: list[str] = []
    for rel in paths:
        subtree = root / rel
        if not subtree.exists() or not subtree.is_dir():
            continue
        for path in sorted(subtree.glob('*.py')):
            try:
                text = path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            match = _SELF_NAME_DOCSTRING_HEADER.match(text)
            if match is None:
                continue
            named = match.group(1)
            if named != path.name:
                mismatches.append(
                    f'{rel_id(path, root)}: docstring header names itself {named!r}, '
                    f'but the published basename is {path.name!r}'
                )

    if mismatches:
        return GuardResult(
            kind='bin-script-self-name-matches-basename',
            ok=False,
            message=(
                f'{len(mismatches)} published coordinator/bin script(s) disagree with '
                f'their own docstring-header self-name: {mismatches}'
            ),
        )
    return GuardResult(kind='bin-script-self-name-matches-basename', ok=True)


def _walk_for_guard(root: Path, params: dict):
    """Shared single-discovery-routine walk for checks that scan the target tree.

    Delegates entirely to `iter_surface_files` (§ module docstring, one discovery
    routine) using whichever file-surface params the caller's guard entry supplied.
    Callers that want the exact percolation file-surface pass the store's own
    include/exclude lists via `params`.

    Fails loud when narrowed: a guard entry supplying NEITHER `include_extensions` NOR
    `shebang_prefixes` while narrowing is in effect (the default -- see below) would
    otherwise silently walk zero files (an empty include set matches nothing, and no
    shebang prefix means no extensionless-shebang fallback either) -- a false-green pass
    for whatever check called this, not a genuine "nothing to find." Raises
    `GuardCheckError` naming both params instead of returning an empty iterator.

    `params['narrow_to_include_extensions']` (default `True`, preserving every existing
    guard entry's behavior byte-for-byte) controls whether the walk narrows to
    `include_extensions`/`shebang_prefixes` at all. Setting it `False` opts a guard entry
    into the SAME admission-inverted full-surface walk `engine.py`'s content-transform
    sweep already uses (§ `surface.py` module docstring, ADMISSION-INVERTED 2026-08-05) --
    every published file is in-surface unless excluded, so an extensionless file with no
    `#!` first line (e.g. `coordinator/bin/coordinator-precommit-foreign-platform-check`,
    `coordinator/bin/static-check`, `coordinator/bin/coordinator-ensure-hooks-fleet`) is no
    longer invisible to a residual-leak guard that opts in. Closes the blind spot named in
    cross-repo/inbox/2026-08-03-coordinator-content-repo-em-percolate-leak-surface-and-gate-gaps.md item
    2: the residual-leak guards previously ALWAYS narrowed regardless of what the entry (or
    an inherited `default_file_surface`) declared, so no store row could ever reach the
    extensionless-shebang-free part of the published surface via this walk.

    `.git`/`.fleet-env` (§ `surface.STRUCTURAL_NEVER_PUBLISHED_PREFIXES`) are excluded
    unconditionally, on top of whatever `exclude_prefixes` the guard entry declares.
    Placed HERE rather than at one caller because the structural fact -- nothing under
    `.git` or a dest-local venv is publish-owned content -- is true of every question the
    shared walk is asked, so a `.git`/`.fleet-env` file reaching ANY of the five call
    sites produces an answer about a tree the run does not own. Per kind:
    `no-residual-pattern` reports a leak in a file that is never published (the observed
    `.git` case -- an extensionless `.git/hooks/pre-commit` carrying a codename is
    admitted by the shebang-sniff leg and fails the gate; the observed `.fleet-env` case
    -- vendored scipy/sklearn/mpmath/joblib source under a dest-local virtualenv);
    `forbidden-script-globs` likewise hits on git-internal hooks; `file-count-delta`
    counts, on the OBSERVED side, files the EXPECTED side computed from the source
    staging tree structurally cannot have, so the exclusion makes the two sides MORE
    symmetric (§ `publish.py`'s `_compute_effective_source_count`, which walks a
    `.git`-free staging tree with the same guard-entry params); `source-glob-present`
    could satisfy a presence assertion from a git-internal file;
    `extensionless-shebang-absent` is scoped to named subtrees, so it is reached only by
    a vendored `.git` beneath one -- an equally false hit; `python-syntax-valid` parses
    third-party site-packages source under `.fleet-env` that is neither this repo's
    content nor valid against this checker's assumptions (the observed live failure:
    `.fleet-env/lib/python3.14/site-packages/joblib/test/test_func_inspect_special_
    encoding.py` is deliberately non-utf-8; several scipy/sklearn/mpmath/mypyc files use
    syntax this `compile()`-based check rejects). No kind LOSES a true finding, because
    no kind's question is ever about `.git` or a dest-local venv.
    """
    narrow = params.get('narrow_to_include_extensions', True)
    if (
        narrow
        and not params.get('include_extensions')
        and not params.get('shebang_prefixes')
        and not params.get('include_basenames')
    ):
        # This message is shared by all four
        # `_walk_for_guard` callers, so it must be true for every kind, not just the two
        # (`forbidden-script-globs`, `no-residual-pattern`) whose empty walk is a false
        # green. `source-glob-present` and `file-count-delta` already fail loudly on an
        # empty walk in the ordinary case, so the message stays kind-agnostic rather than
        # asserting a "silently pass" claim that is false for those two. `include_basenames`
        # (§ item 34) counts as a real admission path too -- a walk narrowed to only an
        # exact-basename list is a deliberate, non-empty question, not the false-green this
        # guard exists to catch.
        raise GuardCheckError(
            "guard walk requires params['include_extensions'], params['shebang_prefixes'], "
            "or params['include_basenames'] when narrowing is in effect (default) -- without "
            "one of the three, the walk would scan zero files, which is indistinguishable "
            "from a genuine empty result. Set params['narrow_to_include_extensions'] to "
            "false to opt into the full admission-inverted surface walk instead."
        )
    walk_key = (
        str(root),
        narrow,
        tuple(params.get('include_extensions') or ()),
        tuple(params.get('shebang_prefixes') or ()),
        tuple(params.get('exclude_prefixes') or ()),
        tuple(params.get('exclude_basenames') or ()),
        tuple(params.get('exclude_globs') or ()),
        tuple(params.get('include_basenames') or ()),
        bool(params.get('top_level_only')),
    )
    return guard_cache.cached_walk(walk_key, lambda: _surface_walk(root, params, narrow))


def _surface_walk(root: Path, params: dict, narrow: bool):
    return iter_surface_files(
        root,
        include_extensions=params.get('include_extensions'),
        shebang_prefixes=params.get('shebang_prefixes'),
        exclude_prefixes=[
            *(params.get('exclude_prefixes') or []),
            *_STRUCTURAL_NEVER_PUBLISHED_PREFIXES,
        ],
        exclude_basenames=params.get('exclude_basenames'),
        exclude_globs=params.get('exclude_globs'),
        # An exact-basename opt-in (§ item 34, `surface.is_in_surface`'s own
        # docstring) for an extensionless file that carries no recognized `#!`
        # first line -- `shebang_prefixes` cannot admit it by content, and the
        # store names it explicitly instead. Default unchanged: no entry, no
        # opt-in, byte-for-byte the prior walk.
        include_basenames=params.get('include_basenames'),
        # Narrows by DEFAULT (byte-for-byte preserving every existing guard entry's
        # behavior -- a guard walk's include_extensions/shebang_prefixes remain a
        # genuine, deliberate FILTER for that guard's specific question by default).
        # A guard entry (or an inherited `default_file_surface`, § run_guard) may opt
        # OUT via `narrow_to_include_extensions: false` to reach the SAME
        # admission-inverted full surface the content-transform sweep already uses
        # (§ surface.py's ADMISSION-INVERTED 2026-08-05) -- this is how a residual-leak
        # guard closes the extensionless-shebang-free blind spot (§ this function's
        # docstring, cross-repo item 2) rather than being structurally unable to ever
        # see that part of the published surface.
        narrow_to_include_extensions=narrow,
        top_level_only=bool(params.get('top_level_only')),
    )


_CHECK_DISPATCH: dict[str, Callable[..., GuardResult]] = {
    'source-dir-absent': check_source_dir_absent,
    'prune-stale-paths': check_prune_stale_paths,
    'dirs-absent': check_dirs_absent,
    'dirs-present': check_dirs_present,
    'forbidden-script-globs': check_forbidden_script_globs,
    'no-residual-pattern': check_no_residual_pattern,
    'file-count-delta': check_file_count_delta,
    'canary-survival': check_canary_survival,
    'source-glob-present': check_source_glob_present,
    'extensionless-shebang-absent': check_extensionless_shebang_absent,
    'python-syntax-valid': check_python_syntax_valid,
    'registration-liveness': check_registration_liveness,
    'bin-script-self-name-matches-basename': check_bin_script_self_name_matches_basename,
    'published-import-resolves': check_published_import_resolves,
}

#: Kinds whose verdict on one path depends on that path alone. Under `scope='staged'` (a
#: `target_root` holding only the paths a diff changed) these evaluate the staged files.
PER_PATH_GUARD_KINDS = frozenset({
    'no-residual-pattern',
    'python-syntax-valid',
    'extensionless-shebang-absent',
    'bin-script-self-name-matches-basename',
    'forbidden-script-globs',
    'dirs-absent',
    'prune-stale-paths',
})

#: Kinds whose verdict needs the whole destination tree (counts, presence, cross-file
#: resolution). A sparse staged tree cannot answer them, so `scope='staged'` skips them.
STRUCTURAL_GUARD_KINDS = frozenset({
    'file-count-delta',
    'canary-survival',
    'dirs-present',
    'registration-liveness',
    'published-import-resolves',
    'source-glob-present',
    'source-dir-absent',
})

VALID_SCOPES = frozenset({'full', 'staged'})

_VALID_SEVERITIES = frozenset({'error', 'info'})

# Guard kinds that MUTATE the target tree as their check action (as opposed to merely
# asserting/reporting on existing state). Review: code-reviewer (Finding 2, P2) --
# `severity: 'info'` is documented as advisory-only ("forces the returned ok to True"),
# which is a coherent story for a pure assertion but incoherent for a kind whose check
# function performs the side effect itself: `check_prune_stale_paths` always deletes
# before `run_guard` ever looks at `severity`, so `severity: 'info'` on it can only ever
# mean the store author BELIEVES they are disabling the deletion -- they are not.
# `run_guard` rejects `severity: 'info'` for every kind in this set (§ that function) so
# an "advisory delete" is a loud authoring error, not a silent execute-anyway. Extend this
# set, not an inline literal, so a future mutating kind is covered by construction.
_SIDE_EFFECTING_KINDS = frozenset({'prune-stale-paths'})


#: `params` keys `_walk_for_guard` (via `iter_surface_files`) understands as file-surface
#: scoping -- the set `run_guard` fills in from `default_file_surface` for a guard entry
#: whose OWN `params` supplies neither `include_extensions` nor `shebang_prefixes` (§
#: cross-repo/inbox/2026-08-03-coordinator-content-repo-em-percolate-leak-surface-and-gate-gaps.md item
#: 1). A guard entry's own params always win key-by-key; this only fills gaps.
_FILE_SURFACE_PARAM_KEYS = (
    'include_extensions',
    'shebang_prefixes',
    'exclude_prefixes',
    'exclude_basenames',
    'exclude_globs',
    'narrow_to_include_extensions',
    'include_basenames',
    'top_level_only',
)


def run_guard(
    target_root: str | Path,
    guard_entry: dict,
    *,
    effective_source_count: int | None = None,
    rename_manifest: RenameManifest | None = None,
    source_root: str | Path | None = None,
    pattern_resolver: Callable[[object], str] | None = None,
    default_file_surface: dict | None = None,
    written_paths: frozenset[str] | None = None,
    protected_literals: list[str] | tuple[str, ...] | None = None,
) -> GuardResult:
    """Run one `guardEntry` (§ Store schema) check spec and return its `GuardResult`.

    Dispatches on `guard_entry['kind']` to the matching `check_*` function. Honors
    `guard_entry['override_env']` first -- if that env var is set (non-empty), the check
    is bypassed entirely and a `bypassed=True`, `ok=True` result is returned without
    running the underlying check (bypass is orthogonal to `severity` -- a bypassed result
    is never further modified). `effective_source_count` is forwarded to
    `file-count-delta` only (§ that check's docstring); `rename_manifest`, when supplied,
    is folded into `guard_entry['params']['paths']` (or `['path']`) before dispatch for
    every check kind that accepts a pre-captured path list, so a stale pre-rename entry
    resolves to its renamed path instead of producing a false-ENOENT (§ module docstring,
    rename-manifest reconciliation).

    `source_root` is the SOURCE tree root (distinct from `target_root`), required only by
    the `source-glob-present` kind -- `GuardCheckError` if that kind is dispatched without
    one. `pattern_resolver`, if supplied, resolves a `no-residual-pattern` entry's
    `params['pattern_source']` (opaque descriptor) into a concrete `params['pattern']`
    before dispatch (§ module docstring, runtime-sourced pattern) -- this module treats
    both `source_root` and `pattern_resolver` opaquely; it never inspects a registry or a
    second tree beyond what the caller hands it.

    `severity` (§ `guardEntry.severity`, § module docstring): the underlying check always
    runs. For `'info'`, the returned result has `ok` forced to `True` (the check's actual
    `message` is preserved) and `severity='info'`; for the default `'error'`, the result
    is returned unchanged apart from stamping `severity='error'`. Any other value raises
    `GuardCheckError`.

    `default_file_surface`, when supplied, is the section's composed `file_surface`
    (§ `store.resolve_target`, base+target dict-merge) -- its `_FILE_SURFACE_PARAM_KEYS`
    are filled into `params` for any key the guard entry's OWN `params` does not already
    supply, key-by-key (the entry's own params always win). Without this, a guard entry
    that omits `include_extensions`/`shebang_prefixes` relies on inheriting the base
    file-surface but never actually gets it -- `_walk_for_guard` either raises
    `GuardCheckError` (neither key present anywhere) or, worse, silently narrows to
    whatever partial params the entry itself declared, leaving the entry unguarded while
    it appears covered by the base declaration (§ cross-repo/inbox/2026-08-03-coordinator-content-repo-em
    -percolate-leak-surface-and-gate-gaps.md item 1).

    `written_paths` is forwarded to `python-syntax-valid` only (§ that check's docstring,
    chunk C3 of docs/plans/2026-08-14-percolate-round-deadlock-and-gate-attribution.md) --
    the rel-id set this round actually wrote, used solely to attribute a failure's message
    as written-this-round vs pre-existing; unsupplied, that check's message degrades to its
    prior undifferentiated wording rather than guessing from mtime.

    `protected_literals` is forwarded to `no-residual-pattern` only (§ `check_no_residual_
    pattern`'s docstring) -- the section's store-declared functional literals, masked out of
    the search before that check's pattern match runs.
    """
    kind = guard_entry.get('kind')
    if kind not in _CHECK_DISPATCH:
        raise GuardCheckError(f'unknown guard kind: {kind!r}')

    override_env = guard_entry.get('override_env')
    if _is_overridden(override_env):
        return GuardResult(kind=kind, ok=True, bypassed=True, message=f'bypassed via {override_env}')

    severity = guard_entry.get('severity', 'error')
    if severity not in _VALID_SEVERITIES:
        raise GuardCheckError(f"invalid severity: {severity!r} (must be one of {sorted(_VALID_SEVERITIES)})")
    if severity == 'info' and kind in _SIDE_EFFECTING_KINDS:
        raise GuardCheckError(
            f"severity 'info' is rejected for kind {kind!r}: this check mutates the "
            f"target tree as its action, and advisory severity can only ever suppress "
            f"the RETURNED ok/pass-fail contribution -- it cannot un-delete what the "
            f"check already did. Remove severity (or set 'error') for this guard entry."
        )

    params = dict(guard_entry.get('params') or {})

    if default_file_surface:
        for fs_key in _FILE_SURFACE_PARAM_KEYS:
            if fs_key not in params and fs_key in default_file_surface:
                params[fs_key] = default_file_surface[fs_key]

    if rename_manifest is not None:
        if 'paths' in params:
            params['paths'] = _resolve_paths(params['paths'], rename_manifest)
        if 'path' in params:
            params['path'] = rename_manifest.resolve(params['path'])

    if kind == 'no-residual-pattern':
        has_pattern = 'pattern' in params
        has_pattern_source = 'pattern_source' in params
        if has_pattern and has_pattern_source:
            raise GuardCheckError(
                "no-residual-pattern params may supply 'pattern' or 'pattern_source', not both"
            )
        if has_pattern_source:
            if pattern_resolver is None:
                raise GuardCheckError(
                    "params.pattern_source requires a pattern_resolver to be threaded in"
                )
            params = {**params, 'pattern': pattern_resolver(params['pattern_source'])}

    check_fn = _CHECK_DISPATCH[kind]
    if kind == 'file-count-delta':
        result = check_fn(target_root, params, effective_source_count=effective_source_count)
    elif kind == 'source-glob-present':
        if source_root is None:
            raise GuardCheckError("source-glob-present requires a source_root")
        result = check_fn(source_root, params)
    elif kind == 'python-syntax-valid':
        result = check_fn(target_root, params, written_paths=written_paths)
    elif kind == 'no-residual-pattern':
        result = check_fn(target_root, params, protected_literals=protected_literals)
    else:
        result = check_fn(target_root, params)
    if kind in _SIDE_EFFECTING_KINDS:
        guard_cache.invalidate()

    if severity == 'info':
        return GuardResult(kind=result.kind, ok=True, message=result.message, severity='info')
    return GuardResult(
        kind=result.kind,
        ok=result.ok,
        message=result.message,
        bypassed=result.bypassed,
        severity=severity,
    )


def run_guards(
    target_root: str | Path,
    guard_entries: list[dict],
    *,
    effective_source_count: int | None = None,
    rename_manifest: RenameManifest | None = None,
    source_root: str | Path | None = None,
    pattern_resolver: Callable[[object], str] | None = None,
    default_file_surface: dict | None = None,
    written_paths: frozenset[str] | None = None,
    protected_literals: list[str] | tuple[str, ...] | None = None,
    scope: str = 'full',
) -> list[GuardResult]:
    """Run every `guardEntry` in `guard_entries` in order, returning all `GuardResult`s.

    Does not short-circuit on first failure -- callers get a full report of every check's
    outcome (a caller wanting fail-fast checks `all(r.ok for r in results)` after the
    fact, or inspects the list directly for the first `ok=False` entry). `source_root`,
    `pattern_resolver`, `default_file_surface`, `written_paths`, and `protected_literals`
    are forwarded unchanged to every `run_guard` call (§ that function's docstring;
    `written_paths` reaches only `python-syntax-valid`, `protected_literals` only
    `no-residual-pattern`).

    `scope='staged'` skips every `STRUCTURAL_GUARD_KINDS` entry, reporting it
    `ok=True, skipped=True` -- never as a pass; `scope='full'` (default) runs everything.
    """
    if scope not in VALID_SCOPES:
        raise GuardCheckError(f'invalid scope: {scope!r} (must be one of {sorted(VALID_SCOPES)})')
    if scope == 'staged':
        for entry in guard_entries:
            kind = entry.get('kind')
            if kind not in _CHECK_DISPATCH:
                raise GuardCheckError(f'unknown guard kind: {kind!r}')
    with guard_cache.guard_run():
        return [
            GuardResult(kind=entry['kind'], ok=True, message='skipped (staged scope)', skipped=True)
            if scope == 'staged' and entry.get('kind') in STRUCTURAL_GUARD_KINDS
            else run_guard(
                target_root,
                entry,
                effective_source_count=effective_source_count,
                rename_manifest=rename_manifest,
                source_root=source_root,
                pattern_resolver=pattern_resolver,
                default_file_surface=default_file_surface,
                written_paths=written_paths,
                protected_literals=protected_literals,
            )
            for entry in guard_entries
        ]
