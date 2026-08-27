"""coordinator_core.bash_guards.commit_tripwires -- in-process Python ports of
Checks 9-11 of ``check_validate_commit`` (coordinator_core.bash_guards.
dispatch_checks), which previously delegated to bash scripts located BY
FILENAME via ``_delegate_bin_check`` / ``_find_bin_script``.

Windows de-bash (2026-07-20, docs/plans/2026-07-19-debash-coordinator-windows.md):
DoE's ``coordinator/bin/check-machine-path-leak.sh`` was renamed to
``check-machine-path-leak.py`` by an earlier de-bash session, and
``coordinator/bin/check-bin-sh-polyglot.sh`` is mid-rename to ``.py`` under the
same campaign's Wave 4a sweep. ``_find_bin_script(name)`` looks up scripts BY
EXACT FILENAME and returns ``""`` (silent no-op) on a miss --
``_delegate_bin_check``'s ``if not script: return`` and ``check_validate_commit``'s
``if leak_script and settings_staged:`` both degrade to "nothing ran," not an
error. **Confirmed dead on this machine at port time**: the
``check-machine-path-leak.sh`` filename no longer exists anywhere in
``coordinator/bin/`` (only the renamed ``.py`` does) -- the settings.json
HARD-BLOCK guard was firing on nobody. Separately, this machine's
``~/.claude/plugins/coordinator-claude/coordinator/bin/`` mirror is empty, so
even ``check-schema-version-bump.sh``/``check-bin-sh-polyglot.sh`` (which still
exist as ``.sh`` in the DoE source repo) were unreachable via
``_find_bin_script``'s fallback rung on this install -- all three delegates
were silently no-op'ing here, not just the renamed one. This module removes
the by-filename bash/subprocess coupling entirely so a future DoE-side rename
cannot silently disable a guard again.

Parity oracles -- DoE coordinator/bin/:
  Port of: check-schema-version-bump.sh (DoE 51851112, 2026-07-21)
  Port of: check-bin-sh-polyglot.sh (DoE 51851112, 2026-07-21)
  check-machine-path-leak.py (still alive, already renamed pre-port)

Posture: preserved EXACTLY from the reference impls. Checks 9 and 10
(``check_schema_version_bump`` / ``check_bin_sh_polyglot``) remain WARN-ONLY --
callers fold their return value into ``check_validate_commit``'s ``warnings``
list, never a deny. Check 11 (``check_machine_path_leak``) remains the one
HARD-BLOCK sink (deny on a settings.json machine-path leak) -- unchanged. No
check is promoted or demoted by this port; only the sub-process-by-filename
plumbing is replaced.

Resolution mechanism, per check (NOT interchangeable -- see each function's
own docstring):
  - Checks 9/10 are DoE-claude-repo-specific structural invariants
    (canonical-structure.yaml / coordinator/bin/ live only in the coordinator
    plugin's own source repo). Their bash originals resolve their OWN plugin
    root from ``$(dirname "${BASH_SOURCE[0]}")`` -- i.e. wherever the
    coordinator plugin happens to be INSTALLED -- and diff THAT repo's own
    staged files, independent of which repo's ``git commit`` triggered the
    dispatcher. This port reproduces that same "always the installed
    coordinator-plugin repo, never the commit's own cwd" semantics, but via
    ``coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()`` (the
    canonical, already-adopted-elsewhere DoE-root resolver with a real
    machine-local-registry ladder) rather than re-deriving the bash
    originals' fragile ``BASH_SOURCE``-relative walk / ``_find_bin_script``'s
    hardcoded-depth-index walk -- both of which this port's own author
    confirmed are dead on this machine (see module docstring above). Using
    the robust resolver is fixing the broken PLUMBING that finds the same
    files, not changing the guards' policy.
  - Check 11 (machine-path-leak) is different: it is scoped to whichever repo
    the ``git commit`` actually targets (``settings.json`` can live in any
    repo, not just DoE-claude). It reuses ``check_validate_commit``'s own
    already-resolved ``staged``/``cwd`` (the target repo's staged-file list
    and the commit's own working directory) -- no DoE-root resolution needed.
  - Check 12 (``check_registration_quad_completeness``, added
    docs/plans/2026-07-25-registration-quad-completeness-gate.md) is a third,
    DIFFERENT resolution shape again: its oracle is the STAGED-DIFF content of
    THIS commit, not the worktree, and not any other repo's tables --
    ``coordinator_core.authz.registration_quad.check_registration_quad()``'s
    live-introspection result is filtered down to only the op-keys this
    commit's own staged ``@register_op("...")`` calls mention (AC17). A
    peer's in-flight, unstaged incomplete registration sitting elsewhere in
    the shared worktree must never deny an unrelated commit.
      Repo-scoping (AC20): this check only evaluates when the committing
      repo's ``git rev-parse --show-toplevel`` is the SAME tree the
      ``coordinator_core`` package currently imported into this process was
      loaded from (``_coordinator_core_repo_root()``) -- Checks 9/10's
      always-the-installed-plugin-repo semantics do not apply here, nor does
      Check 11's always-the-commit's-own-repo semantics: this check needs
      BOTH to agree, because its stage-2 oracle (the live Python tables) is
      whichever ``coordinator_core`` happens to be on ``sys.path``, not
      necessarily the repo being committed. Any mismatch fails open (returns
      ``None``) rather than silently checking the wrong tree's tables against
      the right tree's diff.
      Three-stage gating (AC11, AC19), cheapest first: (1) a staged
      path/content gate -- proceed only if the staged set includes one of the
      three quad-surface files by path, or a staged ``.py`` file anywhere
      under ``coordinator_core/`` whose staged content contains
      ``register_op(`` (NOT a directory allowlist -- 23 of 175 registration
      sites live outside ``coordinator_core/ops/``); (2) a middle tier that
      extracts op-keys from the staged content with a single capture group on
      ``@register_op\\("([^"]+)"\\)`` and does three plain dict lookups
      against the lightweight table modules directly (no
      ``coordinator_core.ops`` package walk) -- if every extracted key is
      already in all three tables, returns clean before paying for; (3) only
      then the expensive full-tree discovery walk
      (``check_registration_quad()``), with its result restricted back down
      to the extracted key set before being reported.
      Disposition is HARD DENY (unlike Checks 9/10's warn-only posture),
      subject to a ``COORDINATOR_OVERRIDE_REGISTRATION_QUAD=1`` environment
      override that downgrades deny to an advisory warning -- consumed
      entirely by the ``dispatch_checks.check_validate_commit`` call site via
      the ``_override()`` convention; this module's own
      ``check_registration_quad_completeness()`` is unconditional and does
      not read that token itself.
      § Known coverage limitation (shared with Checks 9-11, restated here
      because this is the hard-deny check where it matters most): this guard
      fires only for commits made through Claude Code's own ``Bash`` tool
      (the PreToolUse hook dispatch path). A human running ``git commit``
      directly in a terminal, or committing via GitHub Desktop or any other
      non-agent client, bypasses it entirely. CI (qsub-02/03) remains the
      only mechanism that catches a non-agent commit -- this module does not
      close that gap and must not be read as if it does.

Check 13 (`check_staged_pathspec_divergence`, added 2026-07-27 per SC-DR-015)
is a fourth resolution shape: its oracle is a live comparison of two
`git diff` invocations (`--cached` vs plain) scoped to the trailing
pathspec of a `git commit -- <paths>` in THIS Bash command -- neither the
DoE-plugin-repo scope of Checks 9/10, the target-commit-repo scope of Check
11, nor the staged-diff-content scope of Check 12. Disposition is ADVISORY
ONLY (never a hard deny) -- see that function's own module-level comment
block for why this one stays warn/offer rather than promoting straight to
deny.

Spec backlink: pln-registration-quad-completeness-bf0d39
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md
Spec backlink: DoE-claude coordinator/docs/wiki/scoped-safety-commits.md § SC-DR-015
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.git.divergence import diverging_paths as _diverging_paths
from coordinator_core.git.run import run_git
from coordinator_core.bash_guards._command_tokenizer import (
    exceeds_tokenizable_ceiling as _exceeds_tokenizable_ceiling,
)
from coordinator_core.bash_guards._helpers import operator_override_note

# Generator-provenance declaration (generator_provenance.py).
# _log_pathspec_divergence_override appends to
# <git_root>/.git/coordinator-sessions/<session_id>/overrides.log -- inside
# .git, an untracked audit trail, never a tracked repo artifact.
GENERATES = []


def _run_git(args: List[str], cwd: Optional[str] = None, timeout: Optional[float] = None) -> Tuple[int, str]:
    """`(returncode, stdout)` for one local git read, bound by
    `git.run.LOCAL_PLUMBING_BUDGET_SECS`.

    Kept as a named local function rather than inlining `run_git` at each of
    this module's call sites: the `(rc, out)` tuple is what every Check in
    here already destructures, and the two sentinel returncodes are
    unchanged (`-1` timeout, `127` git absent) because the shared runner
    emits the same ones this body used to.

    `timeout` no longer DEFAULTS to a number. It defaulted to 2.0 -- the
    same value the shared seam applies -- and a module-private numeric
    default is precisely the shape G7 of
    `docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md` exists to
    remove: 60+ modules each carrying their own copy of a number nobody
    measured. The parameter itself stays, forwarded, because `run_git`
    treats it as narrow-only -- a caller can ask this read to give up
    sooner, and no value it passes can buy more time than the seam allows.
    """
    result = run_git(args, cwd=cwd, timeout=timeout)
    return result.returncode, result.stdout


def _resolve_doe_coordinator_root() -> Optional[str]:
    """Resolve the installed coordinator plugin's ``coordinator/`` directory,
    via the canonical DoE-root resolver. Returns ``None`` on any resolution
    failure (never raises) -- Checks 9/10 fail open on this, matching the
    bash originals' own "not a git repo at PLUGIN_ROOT" -> exit 2 -> no
    warning-appended fail-open shape (see this module's docstring)."""
    try:
        from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root
    except Exception:
        return None
    try:
        doe_root = coordinator_doe_root()
    except Exception:
        return None
    if not doe_root:
        return None
    candidate = os.path.join(doe_root, "coordinator")
    return candidate if os.path.isdir(candidate) else None


# ---------------------------------------------------------------------------
# Check 9 -- check-schema-version-bump.sh -- SCHEMA-BUMP-TRIPWIRE (warn-only)
# ---------------------------------------------------------------------------

_CANONICAL_FILE = "canonical-structure.yaml"
_VERSION_FILE = "coordinator-schema-version"


def check_schema_version_bump() -> Optional[str]:
    """Port of check-schema-version-bump.sh's ``--staged`` mode. Returns a
    VIOLATION detail string (mirroring the bash script's stdout) when
    ``canonical-structure.yaml`` is staged in the coordinator plugin repo
    without a matching bump to ``coordinator-schema-version``; ``None``
    otherwise (OK, or the guard could not run -- fail open, no warning)."""
    plugin_root = _resolve_doe_coordinator_root()
    if plugin_root is None:
        return None

    rc, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=plugin_root)
    if rc != 0:
        return None
    rc, rel_prefix_out = _run_git(["rev-parse", "--show-prefix"], cwd=plugin_root)
    if rc != 0:
        return None
    rel_prefix = rel_prefix_out.strip()
    # --show-prefix already carries the trailing slash when non-empty; "" when
    # the plugin root IS the git root (both forms concatenate directly).
    rel_canonical = rel_prefix + _CANONICAL_FILE
    rel_version = rel_prefix + _VERSION_FILE

    rc, changed_out = _run_git(["diff", "--cached", "--name-only"], cwd=plugin_root)
    if rc != 0:
        return None
    changed_names = set(l for l in changed_out.splitlines() if l)

    canonical_changed = rel_canonical in changed_names
    version_changed = rel_version in changed_names

    if not canonical_changed:
        return None
    if version_changed:
        return None

    version_path = os.path.join(plugin_root, _VERSION_FILE)
    try:
        current_version = Path(version_path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        current_version = "?"

    return (
        "VIOLATION: {canonical} was modified but {version} was not bumped.\n"
        "  Consumers rely on the version integer to detect schema drift. Every structural\n"
        "  change to canonical-structure.yaml must be accompanied by a version increment.\n"
        "  Fix: increment the integer in {rel_version} (currently: {current}) and re-stage."
    ).format(
        canonical=_CANONICAL_FILE,
        version=_VERSION_FILE,
        rel_version=rel_version,
        current=current_version,
    )


# ---------------------------------------------------------------------------
# Check 10 -- check-bin-sh-polyglot.sh -- BIN-SH-POLYGLOT-TRIPWIRE (warn-only)
# ---------------------------------------------------------------------------

_TRAMPOLINE = '\'\'\'\'exec "$(command -v python3 || command -v python || command -v py)" "$0" "$@" #\'\'\''

#: Guard-self-skip -- a guard that DETECTS the trampoline must carry the
#: TRAMPOLINE string as a data value, so it would otherwise self-classify as
#: in-class. Covers the historical .sh name and its Wave 4a rename target
#: (either may be on disk at port time) plus the repo-wide sibling guard, which
#: quotes the same literal for the same reason. Membership is by construction,
#: not convenience: only a checker whose subject IS the trampoline belongs here.
#:
#: Three surfaces carry this exemption independently: this set,
#: _GUARD_SELF_SKIP_BASENAMES in coordinator/bin/check-bin-sh-polyglot.py, and
#: EXCLUDED_TRAMPOLINE_DOC_FILES in
#: coordinator/bin/tests/test_no_bin_polyglot_invariant.py (keyed on
#: repo-relative path). They are NOT strictly equal and are not meant to be:
#: only this set retains "check-bin-sh-polyglot.sh", the pre-rename name, so a
#: tree that has not yet taken the rename stays exempt -- inert wherever the
#: rename has landed, which is why that asymmetry is deliberate rather than
#: drift. The enforced invariant is therefore agreement on the LIVE-FILE
#: SUBSET, not strict set equality: after dropping entries naming files absent
#: from disk and normalizing basename-vs-repo-relative keying, all three must
#: be identical, or one surface reads green while another fires on the same
#: file. Mechanically checked by TestGuardSelfSkipCrossSetAgreement in
#: tests/test_commit_tripwires.py.
#:
#: Basename keying is safe only because this guard's scan domain is
#: non-recursive over one directory (coordinator/bin/ -- see the ``"/" in tail``
#: filter below), so a basename collision is impossible; widening the scan
#: domain would require re-keying this set on repo-relative path, as
#: sh-suffix-polyglot-baseline.txt already is.
_SELF_SKIP_BASENAMES = {
    "check-bin-sh-polyglot.sh",
    "check-bin-sh-polyglot.py",
    "check-sh-suffix-polyglot.py",
}


def check_bin_sh_polyglot() -> Optional[str]:
    """Port of check-bin-sh-polyglot.sh's ``--staged`` mode. Returns a
    VIOLATION detail string (mirroring the bash script's stdout) listing any
    staged coordinator/bin/ file that is in the sh/python polyglot class
    (has the trampoline within its first 20 lines) but is missing the
    ``#!/bin/sh`` line-1 shebang; ``None`` otherwise (OK, or the guard could
    not run)."""
    plugin_root = _resolve_doe_coordinator_root()
    if plugin_root is None:
        return None
    bin_dir = os.path.join(plugin_root, "bin")
    if not os.path.isdir(bin_dir):
        return None

    rc, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=plugin_root)
    if rc != 0:
        return None
    rc, rel_prefix_out = _run_git(["rev-parse", "--show-prefix"], cwd=plugin_root)
    if rc != 0:
        return None
    rel_prefix = rel_prefix_out.strip()
    bin_rel_prefix = rel_prefix + "bin/"

    rc, git_root_out = _run_git(["rev-parse", "--show-toplevel"], cwd=plugin_root)
    if rc != 0:
        return None
    git_root = git_root_out.strip()

    rc, staged_out = _run_git(["diff", "--cached", "--name-only"], cwd=plugin_root)
    if rc != 0:
        return None

    candidates: List[str] = []
    for rel in staged_out.splitlines():
        if not rel.startswith(bin_rel_prefix):
            continue
        tail = rel[len(bin_rel_prefix):]
        if "/" in tail:
            continue  # not directly under bin/ -- out of scope
        if tail in _SELF_SKIP_BASENAMES:
            continue
        candidates.append(rel)

    offenders: List[str] = []
    for rel in candidates:
        abs_path = os.path.join(git_root, rel) if git_root else os.path.join(plugin_root, "..", rel)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                head_lines = [next(fh, "") for _ in range(20)]
        except OSError:
            # Warn-only check (Check 10 never denies) -- an unreadable staged
            # file just drops out of the polyglot-class scan for this commit;
            # skip it rather than surface a read failure for a file git's own
            # diff --cached already reported as present in the index.
            continue
        header = "".join(head_lines)
        has_trampoline = _TRAMPOLINE in header
        if not has_trampoline:
            continue  # not in the polyglot class
        first_line = head_lines[0].rstrip("\n").rstrip("\r") if head_lines else ""
        if first_line != "#!/bin/sh":
            offenders.append("  {}  (missing #!/bin/sh on line 1)".format(abs_path))

    if not offenders:
        return None

    return (
        "VIOLATION: BIN-SH-POLYGLOT-INVARIANT — the following coordinator/bin/ file(s)\n"
        "  are in the sh/python polyglot class but are missing #!/bin/sh on line 1 and/or\n"
        "  the verbatim trampoline line.\n"
        "  See: docs/wiki/cross-platform-shell-portability.md § sh/python trampoline\n"
        "  Plan: docs/plans/2026-06-18-bin-cli-sh-shebang-polyglot.md\n\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Check 11 -- check-machine-path-leak.py -- machine-path-leak (HARD block)
# ---------------------------------------------------------------------------

_SETTINGS_PATTERNS = [
    re.compile(r"^/Users/[^/]+/"),
    re.compile(r"^/home/[^/]+/"),
    re.compile(r"^C:[/\\]Users[/\\]"),
    re.compile(r"^X:[/\\]"),
    re.compile(r"^E:[/\\]"),
]


def _is_machine_abs(val: str) -> bool:
    return any(p.search(val) for p in _SETTINGS_PATTERNS)


def _walk_json(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = (path + "." + k) if path else k
            yield from _walk_json(v, child)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_json(v, path + "[" + str(i) + "]")
    elif isinstance(obj, str):
        if _is_machine_abs(obj):
            yield (path, obj)


def _read_settings_content(rel_path: str, cwd: Optional[str]) -> Optional[str]:
    """Read a staged settings.json's content -- from disk if present, else the
    git index -- mirroring check-machine-path-leak.py's ``_read_file_or_index``.
    Scoped to ``cwd`` (the commit's own working directory), unlike Checks 9/10."""
    abs_path = os.path.join(cwd, rel_path) if cwd else rel_path
    if os.path.isfile(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None
    rc, out = _run_git(["show", ":{}".format(rel_path)], cwd=cwd)
    if rc == 0:
        return out
    return None


def check_machine_path_leak(rel_path: str, cwd: Optional[str] = None) -> Optional[str]:
    """Port of check-machine-path-leak.py's settings.json HARD-block scan
    (``_check_settings_json``). Returns a detail string (mirroring the
    original's stderr violation lines) if ``rel_path`` (a staged settings.json)
    contains a machine-specific absolute-path leaf value; ``None`` if clean or
    unparseable/unreadable (fail open -- matches the original's JSONDecodeError
    branch, which DOES flag a hard_violation on unparseable JSON; preserved
    below)."""
    import json

    content = _read_settings_content(rel_path, cwd)
    if content is None:
        return None

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return "ERROR — failed to parse {} as JSON: {}".format(rel_path, e)

    lines: List[str] = []
    for leaf_path, leaf_val in _walk_json(data):
        lines.append("VIOLATION: {}: machine-specific path in JSON leaf".format(rel_path))
        lines.append("  Leaf path : {}".format(leaf_path))
        lines.append("  Value     : {}".format(leaf_val))
        lines.append(
            "  Remedy    : machine-specific paths must live in gitignored\n"
            "              settings.local.json or machine-local registry,\n"
            "              not in tracked settings.json"
        )

    if not lines:
        return None
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Check 12 -- registration-quad-completeness -- REGISTRATION-QUAD-INVARIANT
# (HARD block, override-gated). See module docstring above for the full
# resolution-mechanism writeup; this section is the implementation only.
# ---------------------------------------------------------------------------

# Review: code-reviewer (Finding 1) -- derived from
# `registration_quad._SURFACE_FILES` (the single source of truth for which file
# each of the five quad surfaces lives in) rather than re-listed here, so the two
# files cannot drift again the way they did when `_EAGER_OP_MODULES` was added as
# a fifth surface to `registration_quad.py` without a matching update here. The
# literal fallback is a fail-safe only -- it must stay in sync with
# `registration_quad._SURFACE_FILES`'s own four target paths if the import ever
# fails, not a second independent enumeration to maintain in practice.
try:
    from coordinator_core.authz.registration_quad import _SURFACE_FILES as _QUAD_SURFACE_FILES

    _REGISTRATION_SURFACE_FILES = frozenset(_QUAD_SURFACE_FILES.values())
except Exception:
    _REGISTRATION_SURFACE_FILES = frozenset(
        {
            "coordinator_core/authz/classification.py",
            "coordinator_core/op_scopes.py",
            "coordinator_core/ops/_registry_map.py",
            "coordinator_core/ops/__init__.py",
        }
    )

# Review: code-reviewer (Finding 3) -- anchored to the start of a (stripped) line so
# a docstring/comment that merely QUOTES this decorator shape as a usage example
# (e.g. this module's own docstring, or ipc.py's register_op() docstring) does not
# get treated as a real registration. Residual, acknowledged rather than silently
# implied covered: a single-quoted `@register_op('key')` and the documented
# direct-call form `register_op("key", handler)` (no leading `@`) both stay
# invisible to this regex -- an AST scanner would close that gap but is explicit
# anti-scope per the plan. A real op-key that happens to be quoted this way,
# alone on its own line, in staged docstring/comment prose is still a possible
# false-candidate; this narrows but does not eliminate that risk.
_REGISTER_OP_KEY_RE = re.compile(r'^@register_op\("([^"]+)"\)')


def _coordinator_core_repo_root() -> Optional[str]:
    """Directory containing the ``coordinator_core`` package currently
    imported into THIS process -- i.e. wherever this very module lives, one
    level up from the package itself. Its own function (not inlined) so
    tests can monkeypatch it directly to simulate a same-tree or
    cross-tree commit without needing a second real git clone (AC20)."""
    try:
        import coordinator_core
    except Exception:
        return None
    module_file = getattr(coordinator_core, "__file__", None)
    if not module_file:
        return None
    package_dir = os.path.dirname(os.path.abspath(module_file))
    return os.path.dirname(package_dir)


def _same_tree(path_a: str, path_b: str) -> bool:
    return os.path.normcase(os.path.realpath(path_a)) == os.path.normcase(os.path.realpath(path_b))


# Review: code-reviewer (Finding 1) -- one batched `git grep --cached` subprocess
# across every candidate path, replacing a `git show :<path>` spawned PER staged
# file. `--cached` searches the INDEX (staged blobs), never the working tree --
# preserving the same "judges the commit, not the worktree" guarantee (AC17) the
# old per-file `git show` gave, at O(1) subprocess spawns instead of O(N).
def _grep_cached_lines(needle: str, paths: List[str], cwd: Optional[str]) -> Optional[List[str]]:
    """Lines from the STAGED (index) content of ``paths`` containing the fixed
    string ``needle``, via one ``git grep --cached`` call. Returns ``[]`` when
    nothing matches (git grep exits 1 for "no match" -- not an error, unlike
    every other ``_run_git`` call site in this module). Returns ``None`` only
    when the invocation itself could not run (fail open)."""
    if not paths:
        return []
    rc, out = _run_git(["grep", "--cached", "--no-color", "-F", "-h", needle, "--", *paths], cwd=cwd)
    if rc == 1:
        return []
    if rc != 0:
        return None
    return [l for l in out.splitlines() if l]


def _prune_baselined_classification(violation, baseline):
    """Drop a violation's `OP_CLASSIFICATION` leg when its op-key sits in the frozen
    known-debt baseline, returning None if nothing punishable remains.

    The baseline (`registration_quad._KNOWN_UNCLASSIFIED_OPS_DEBT`) is a PM-ratified
    tolerated-debt set covering ONE surface only, so this must never suppress a
    baselined op's missing `_OP_KEY_SCOPE` or `OP_MODULE_MAP` entry — those are still
    live defects on an op that merely happens to be unclassified.

    Negative-spec: this is the commit-time half of AC4's single-source-of-truth
    requirement. Without it the gate hard-denies any commit that merely re-stages one
    of the baselined ops' `@register_op(...)` lines (a move, a rename, a reformat) --
    a false deny on debt the plan explicitly placed out of scope.
    """
    if violation.op_key not in baseline:
        return violation
    kept_missing = tuple(s for s in violation.surfaces_missing if s != "OP_CLASSIFICATION")
    if not kept_missing:
        return None
    kept_files = tuple(
        (surface, path)
        for surface, path in violation.missing_surface_files
        if surface != "OP_CLASSIFICATION"
    )
    # Review: code-reviewer (Finding 4) -- dropped the no-op
    # `surfaces_present=violation.surfaces_present` kwarg; `dataclasses.replace`
    # already preserves any field not passed.
    return dataclasses.replace(
        violation,
        surfaces_missing=kept_missing,
        missing_surface_files=kept_files,
    )


def check_registration_quad_completeness(cwd: Optional[str] = None) -> Optional[str]:
    """Hard-deny detail string when this commit's own staged content lands an
    op-key in ``@register_op(...)`` without also landing it in all three of
    ``OP_CLASSIFICATION``, ``_OP_KEY_SCOPE``, and ``OP_MODULE_MAP`` -- see
    ``coordinator_core.authz.registration_quad`` for the underlying pure
    set-diff this reuses. Returns ``None`` when clean, when the check cannot
    run (fails open, mirroring Checks 9/10/11's own failure posture), or --
    per AC20 -- when the committing repo is not the same tree the imported
    ``coordinator_core`` was loaded from.

    Three cheap-to-expensive gates, in order (AC11, AC19): repo-scope, then
    staged-path/content, then staged-op-key-vs-tables, before ever paying for
    the full-tree discovery walk. See module docstring for the full writeup.
    """
    rc, top_out = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if rc != 0:
        return None
    committing_root = top_out.strip()
    if not committing_root:
        return None

    core_root = _coordinator_core_repo_root()
    if core_root is None or not _same_tree(committing_root, core_root):
        return None

    rc, staged_out = _run_git(["diff", "--cached", "--name-only"], cwd=committing_root)
    if rc != 0:
        return None
    staged = [l for l in staged_out.splitlines() if l]
    if not staged:
        return None

    staged_py_under_core = [
        f for f in staged if f.startswith("coordinator_core/") and f.endswith(".py")
    ]
    surface_staged = any(f in _REGISTRATION_SURFACE_FILES for f in staged)

    # Review: code-reviewer (Finding 1) -- test the free check (surface_staged, from
    # `staged` already in hand) and the "no .py under coordinator_core/ at all"
    # case BEFORE spending a single subprocess on content. A large non-registration
    # refactor that never touches a quad-surface file and stages no .py under
    # coordinator_core/ now pays zero subprocess spawns for this check, not N.
    if not surface_staged and not staged_py_under_core:
        return None  # stage 1: nothing under coordinator_core/ staged at all

    matched_lines: List[str] = []
    if staged_py_under_core:
        grepped = _grep_cached_lines("register_op(", staged_py_under_core, committing_root)
        if grepped is None:
            return None  # grep itself failed to run -- fail open
        matched_lines = grepped

    if not (surface_staged or matched_lines):
        return None  # stage 1: no registration surface staged in this commit

    op_keys: set = set()
    for line in matched_lines:
        m = _REGISTER_OP_KEY_RE.match(line.strip())
        if m:
            op_keys.add(m.group(1))

    if not op_keys:
        # A quad-surface file was staged (e.g. a comment edit in
        # classification.py) but no staged file actually registers an op in
        # this commit -- nothing for this check to report.
        return None

    try:
        from coordinator_core.authz.classification import OP_CLASSIFICATION
        from coordinator_core.op_scopes import _OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP
    except Exception:
        OP_CLASSIFICATION = _OP_KEY_SCOPE = OP_MODULE_MAP = None  # type: ignore[assignment]

    # Review: code-reviewer (Finding 1) -- the fast path must also check the
    # fifth surface (`_EAGER_OP_MODULES`), keyed by an op's `OP_MODULE_MAP`
    # module path, not just the original three tables. Without this, an op
    # complete in `OP_CLASSIFICATION`/`_OP_KEY_SCOPE`/`OP_MODULE_MAP` but
    # missing from `_EAGER_OP_MODULES` returned clean here without ever
    # reaching `check_registration_quad()` below -- the exact live gap
    # (`roadmap.link_stubs`, 2026-08-05) this check exists to close. A failure
    # to resolve the live eager-module set here is NOT treated as "clean" --
    # it falls through to the full `check_registration_quad()` call instead,
    # which has its own independent fail-open posture.
    if OP_CLASSIFICATION is not None:
        try:
            from coordinator_core.ops import _EAGER_OP_MODULES

            _eager_module_paths = frozenset(mp for mp, _note in _EAGER_OP_MODULES)
        except Exception:
            _eager_module_paths = None
        if _eager_module_paths is not None and all(
            k in OP_CLASSIFICATION
            and k in _OP_KEY_SCOPE
            and k in OP_MODULE_MAP
            and OP_MODULE_MAP[k] in _eager_module_paths
            for k in op_keys
        ):
            return None  # stage 1.5: every staged key already complete on all five surfaces

    try:
        from coordinator_core.authz.registration_quad import (
            _KNOWN_UNCLASSIFIED_OPS_DEBT,
            check_registration_quad,
            prune_known_incomplete,
        )
    except Exception:
        return None

    # Review: code-reviewer (Finding 2) -- this call runs the full discovery walk
    # (re-imports every op module reachable from coordinator_core.ops, plus the
    # three quad-surface tables) with no params. A syntax/import error in any of
    # those -- the exact shape of a commit mid-editing this guard's own target
    # population -- must not escape this HARD-DENY check uncaught; fail open,
    # matching the docstring's stated posture and the module-import guards above.
    try:
        violations = check_registration_quad()
        relevant = []
        for v in violations:
            if v.op_key not in op_keys:
                continue
            pruned = _prune_baselined_classification(v, _KNOWN_UNCLASSIFIED_OPS_DEBT)
            if pruned is None:
                continue
            # Also drop any surfaces recorded in the fuller
            # `_KNOWN_INCOMPLETE_REGISTRATIONS` ledger (registration_quad.py,
            # 2026-08-11) -- forgives exactly the recorded gap, never the op
            # wholesale (see that ledger's own module-level comment).
            pruned = prune_known_incomplete(pruned)
            if pruned is None:
                continue
            relevant.append(pruned)
    except Exception:
        return None
    if not relevant:
        return None

    lines = [
        "VIOLATION: REGISTRATION-QUAD-INVARIANT — the following op(s) registered by "
        "this commit's staged changes",
        "  are missing required quad-surface entries. Registering a coordinator_core",
        "  op requires landing the same op-key in @register_op(...), OP_CLASSIFICATION,",
        "  _OP_KEY_SCOPE, and OP_MODULE_MAP together.",
        "  See: coordinator_core/authz/registration_quad.py",
        "  Plan: docs/plans/2026-07-25-registration-quad-completeness-gate.md",
        "",
    ]
    for v in relevant:
        lines.append("  {}".format(v.op_key))
        lines.append(
            "    present: {}".format(", ".join(v.surfaces_present) if v.surfaces_present else "(none)")
        )
        for surface, path in v.missing_surface_files:
            lines.append("    missing: {} (add an entry to {})".format(surface, path))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Check 13 -- staged-pathspec-divergence -- STAGED-PATHSPEC-DIVERGENCE
# (advisory/offer, override-gated; NEVER denies).
#
# `git commit -- <paths>` commits WORKTREE content for `<paths>` and bypasses
# the index. On a shared tree, a session that deliberately staged a partial
# hunk (`git apply --cached`, hand-staged subset) and then commits with a
# trailing pathspec silently discards its own staging and absorbs whatever a
# concurrent session left in the worktree at those paths. Empirically
# confirmed in commit 506748a0 (claude-klabauter, 2026-07-27): an EM detected
# the entanglement, unstaged the file, filtered the diff to its own hunk,
# `git apply --cached`'d it so the index held exactly the right content --
# then ran `git commit -m ... -- <paths>` and discarded all of it; two hunks
# belonging to a concurrent session landed under the wrong subject.
#
# Ruling + full empirical writeup: SC-DR-015, canonical text at DoE-claude's
# `coordinator/docs/wiki/scoped-safety-commits.md` § SC-DR-015. That ruling's
# own "Discharge" section names this exact guard as the artifact that makes
# the ruling unnecessary to remember: "a PreToolUse check that sees
# `git commit -- <paths>` while the index and worktree disagree for those
# paths, and offers the index-honouring form (design-as-offers: lead with the
# better command, do not scold)."
#
# Disposition (deliberate, not the Check-12 default): ADVISORY ONLY, never a
# HARD DENY. Check 12 (registration-quad) can hard-deny because its oracle is
# a pure set-diff over this commit's own staged content -- false-positive
# rate is ~0 by construction. This check's oracle is a live git-state
# comparison (two `git diff` invocations) with real false-positive surface --
# e.g. a session that intentionally wants worktree content for an unrelated
# path swept into the same commit line. That FP profile is exactly what
# SC-DR-003's warn-first soak gate exists for (see scoped-safety-commits.md's
# own Check-5/strict-mode precedent, which stayed warn-only through an
# entire soak cycle before any promotion was considered) -- promoting this
# straight to a hard deny with no soak period would risk wedging a
# legitimate commit shape this check cannot yet distinguish from the
# dangerous one. An advisory that leads with the safer command captures the
# same discharge value (the operator no longer has to remember the rule --
# the tool tells them) without that risk.
# ---------------------------------------------------------------------------

_GIT_COMMIT_SEG_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*=\S*\s+)*git\s+commit(\s|$)"
)


def _extract_commit_trailing_pathspecs(seg: str) -> Optional[List[str]]:
    """Tokenize one already-segmented shell fragment (quote-aware within its
    own quoting -- `seg` is expected to already be one output element of
    `_awk_quote_aware_split`) and, if it invokes `git commit` with a
    trailing `--` pathspec separator, return the pathspec tokens following
    the LAST unquoted `--`. Returns ``None`` when:

      - the segment does not invoke `git commit` at all (no trailing `--`
        to reason about), or
      - `--` is present but followed by zero tokens. `git commit -- ` with
        nothing after it is NOT a scoped commit -- it is git's own
        "no pathspec given" form (commits whatever is staged, same as no
        `--` at all). Treating this as "zero dangerous paths" would be
        correct only by accident; the actual reason it's safe to skip is
        that there is no worktree-vs-index SUBSTITUTION risk when no path
        is being scoped in the first place -- the same semantics as the
        no-`--`-at-all case, not a special "empty means nothing" case.
      - the fragment fails to tokenize (unterminated quote spanning a
        `_awk_quote_aware_split` boundary, etc.) -- fails CLOSED to
        not-applicable; this check can only narrow itself out of firing,
        never widen into a false ADVISORY on a segment it could not safely
        parse.
    """
    if not _GIT_COMMIT_SEG_RE.match(seg):
        return None
    if _exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `None` is the documented not-applicable branch; this check
        # is advisory-only, so the ceiling can only withhold an OFFER.
        return None
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return None
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    if i + 1 >= len(tokens) or tokens[i] != "git" or tokens[i + 1] != "commit":
        return None
    rest = tokens[i + 2:]
    if "--" not in rest:
        return None
    last_dd = len(rest) - 1 - rest[::-1].index("--")
    paths = rest[last_dd + 1:]
    if not paths:
        return None
    return paths


from coordinator_core.bash_guards._override_log_path import _override_log_path


def _log_pathspec_divergence_override(cmd: str, cwd: Optional[str], session_id: str) -> None:
    """Append one audit line to `.git/coordinator-sessions/<sid>/overrides.log`,
    same path convention and one-line format `check_blanket_git_add` uses for
    its own `COORDINATOR_OVERRIDE_BLANKET_ADD` audit trail -- this is the
    override-logging convention peer guards in this codebase follow, not a
    new one invented for this check."""
    try:
        rc_root, root_out = _run_git(["rev-parse", "--show-toplevel"], cwd)
        git_root = root_out.strip() if rc_root == 0 else (cwd or "")
        if not git_root:
            return
        override_log = _override_log_path(git_root, session_id)
        if override_log is None:
            return
        with open(override_log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "%s | %s | OVERRIDE-PATHSPEC-DIVERGENCE | %s\n"
                % (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    session_id or "no-session",
                    cmd[:120],
                )
            )
    except OSError as exc:
        # Audit-log write failed -- the override itself still proceeds (this
        # check is advisory-only regardless), but the override is now
        # unrecorded, so surface it, mirroring check_blanket_git_add's own
        # failure-mode print.
        print(
            "staged-pathspec-divergence: failed to write override audit log: %s" % exc,
            file=sys.stderr,
        )


def check_staged_pathspec_divergence(
    cmd: str,
    cwd: Optional[str] = None,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Advisory (never denies) detail string when `cmd` contains a
    `git commit -- <paths>` whose STAGED (index) content at one or more of
    `<paths>` differs from the WORKTREE content there -- the exact condition
    under which the trailing pathspec silently discards staged content and
    substitutes the worktree instead (SC-DR-015). Returns ``None`` when
    clean, not applicable (see `_extract_commit_trailing_pathspecs`), or when
    `COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE=1` is set (env-only, NOT an
    inline prefix -- consistent with every other `COORDINATOR_OVERRIDE_*` in
    this codebase), in which case the bypass is logged via
    `_log_pathspec_divergence_override` before returning ``None``.

    Reuses `_awk_quote_aware_split` (imported lazily from `dispatch_checks`
    to avoid a module-load-time circular import -- `dispatch_checks` imports
    this module at its own top level) rather than re-solving the same
    `;`/`&`/`|` quote-state segmenting problem `check_blanket_git_add`
    already solved.

    Design-as-offers (global `~/.claude/CLAUDE.md` § Implementation
    Standards): the returned string leads with the safer command, not a
    scold -- see the literal text below.
    """
    if not cmd:
        return None

    try:
        from coordinator_core.bash_guards.dispatch_checks import (
            _awk_quote_aware_split,
            _crlf_strip,
            _join_backslash_newlines,
            _override,
        )
    except Exception:
        # Fail open -- cannot safely re-derive the shared segment splitter
        # here without risking a divergent, un-reviewed re-implementation.
        return None

    command = _crlf_strip(cmd)
    command = _join_backslash_newlines(command)

    all_paths: List[str] = []
    for seg in _awk_quote_aware_split(command):
        if not seg.strip():
            continue
        paths = _extract_commit_trailing_pathspecs(seg)
        if paths:
            all_paths.extend(paths)

    if not all_paths:
        return None

    diverging = _diverging_paths(all_paths, cwd)

    if not diverging:
        return None

    if _override("COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE"):
        _log_pathspec_divergence_override(cmd, cwd, session_id)
        return None

    paths_list = ", ".join(diverging)
    return (
        "OFFER: this `git commit` has a trailing `--` pathspec covering {paths}, "
        "and the STAGED content there differs from the WORKTREE content -- the "
        "trailing pathspec reads the worktree, so this commit would silently "
        "discard what you staged there and substitute the worktree instead "
        "(SC-DR-015).\n\n"
        "A bare no-pathspec commit is NOT the fix -- the shared index can gain a "
        "peer's staged file between your check and your commit (that TOCTOU has "
        "hit for real -- 7 files swept, including another "
        "session's in-flight agent definitions). Usually simplest: don't "
        "partial-stage on a shared "
        "tree at all -- make the worktree at {paths} match only your change, "
        "then commit normally. Genuinely diverged and need to commit anyway? "
        "Isolate the commit in a private GIT_INDEX_FILE (write-tree + "
        "commit-tree) rather than the shared index (SC-DR-015).\n\n"
        "{override_note}"
    ).format(
        paths=paths_list,
        override_note=operator_override_note(
            "COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE", payload=payload
        ),
    )
