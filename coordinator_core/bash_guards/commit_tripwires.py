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
``~/.claude/plugins/coordinator/bin/`` mirror is empty, so
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
  - Checks 9/10 are coordinator-content-repo-repo-specific structural invariants
    (canonical-structure.yaml / coordinator/bin/ live only in the coordinator
    plugin's own source repo). Their bash originals resolve their OWN plugin
    root from ``$(dirname "${BASH_SOURCE[0]}")`` -- i.e. wherever the
    coordinator plugin happens to be INSTALLED -- and diff THAT repo's own
    staged files, independent of which repo's ``git commit`` triggered the
    dispatcher. This port reproduces that same "always the installed
    coordinator-plugin repo, never the commit's own cwd" semantics, but via
    ``coordinator_core.content_root.read_content_root()`` (the canonical
    content-root resolver with a real machine-local-registry ladder) rather than re-deriving the bash
    originals' fragile ``BASH_SOURCE``-relative walk / ``_find_bin_script``'s
    hardcoded-depth-index walk -- both of which this port's own author
    confirmed are dead on this machine (see module docstring above). Using
    the robust resolver is fixing the broken PLUMBING that finds the same
    files, not changing the guards' policy.
  - Check 11 (machine-path-leak) is different: it is scoped to whichever repo
    the ``git commit`` actually targets (``settings.json`` can live in any
    repo, not just coordinator-content-repo). It reuses ``check_validate_commit``'s own
    already-resolved ``staged``/``cwd`` (the target repo's staged-file list
    and the commit's own working directory) -- no content-root resolution needed.
  - Check 12 (``check_registration_quad_completeness``, added
    docs/plans/2026-07-25-registration-quad-completeness-gate.md) is a third,
    DIFFERENT resolution shape again: its oracle is the post-commit tree of the
    committing repo (index blob where staged, HEAD blob elsewhere), judged by
    ``registration_quad_static.registration_violations`` over one batched
    ``git cat-file --batch`` read of the staged ``coordinator_core/*.py``
    candidates plus the five surface files. It never imports the live tables, so
    the verdict holds under any engine root, and an incomplete registration
    elsewhere in the shared worktree never denies an unrelated commit. The
    staged list is the one ``check_validate_commit`` already holds; a commit
    staging no ``coordinator_core/*.py`` and no surface file spawns nothing.
    A surface file that cannot be read or parsed fails open here.
      Disposition is HARD DENY (unlike Checks 9/10's warn-only posture),
      subject to a ``COORDINATOR_OVERRIDE_REGISTRATION_QUAD=1`` environment
      override that downgrades deny to an advisory warning -- consumed
      entirely by the ``dispatch_checks.check_validate_commit`` call site via
      the ``_override()`` convention; this module's own
      ``check_registration_quad_completeness()`` is unconditional and does
      not read that token itself.
      § Known coverage limitation: this guard fires for commits made through
      Claude Code's own ``Bash`` tool (the PreToolUse hook dispatch path) and,
      through the shared cores, on ``ceremony.commit_v2``. A human running
      ``git commit`` directly in a terminal, or committing via GitHub Desktop
      or any other non-agent client, bypasses both. CI (qsub-02/03) remains the
      only mechanism that catches a non-agent commit.

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
Spec backlink: coordinator-content-repo coordinator/docs/wiki/scoped-safety-commits.md § SC-DR-015
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core import machine_path_leak
from coordinator_core.git.divergence import (
    DivergenceCheckFailed,
    diverging_paths as _diverging_paths,
)
from coordinator_core.git.commit_trailers import (
    _paragraph_is_trailer_shaped as _ct_paragraph_is_trailer_shaped,
    _split_paragraphs as _ct_split_paragraphs,
)

#: Emitted when the divergence read is INDETERMINATE, never when it is clean.
#: Register (docs/wiki/guard-messaging.md): one fact, once, plus a terse
#: alternative. It names what could not be established rather than asserting a
#: divergence that was never measured -- claiming one would be the mirror defect
#: of the silence it replaces.
_INDETERMINATE_DIVERGENCE_OFFER = (
    "OFFER: this `git commit` has a trailing `--` pathspec covering {paths}, and "
    "whether the STAGED content there differs from the WORKTREE content could NOT "
    "be determined -- the index was unreadable, usually because a peer holds it. "
    "This is not a clean result: a trailing pathspec reads the worktree, so if "
    "those paths are staged the commit discards what you staged (SC-DR-015).\n\n"
    "Re-run the command -- index contention is transient and the check settles on "
    "a retry. If it keeps failing, make the worktree at {paths} match only your "
    "change so staged and worktree agree, and the question stops mattering.\n\n"
    "{override_note}"
)
#: Emitted when EVERY path the trailing pathspec covers is an index-only removal
#: (`git rm --cached`, file kept on disk) and nothing else diverges. The generic
#: offer's remedies all assume staged content to preserve; here the staged side
#: is a deletion, and a bare no-pathspec commit is the one form that commits it.
#: Exit code proves nothing (the wrong form also exits 0), so the offer ends on
#: the `ls-files --error-unmatch` check that does.
_INDEX_ONLY_REMOVAL_OFFER = (
    "OFFER: this `git commit` has a trailing `--` pathspec covering {paths}, all "
    "staged as an INDEX-ONLY removal (`git rm --cached`) with the file still on "
    "disk. A trailing pathspec reads the worktree, so it re-`add`s the file and "
    "silently reverts the untrack -- exit 0, subject line claiming the opposite "
    "(SC-DR-015).\n\n"
    "Commit the index as staged instead: `git diff --cached --name-only` to "
    "confirm only your paths are staged, then `git commit -m ...` with no "
    "pathspec. If a peer's files are staged too, isolate the commit in a private "
    "GIT_INDEX_FILE (read-tree HEAD, `git rm --cached -- {paths}`, write-tree + "
    "commit-tree) rather than the shared index.\n\n"
    "Verify afterwards: `git ls-files --error-unmatch -- {paths}` must exit "
    "non-zero. Exit 0 means the path is still tracked, whatever the commit "
    "reported.\n\n"
    "{override_note}"
)
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


def _resolve_plugin_content_root() -> Optional[str]:
    """Resolve the installed coordinator plugin's content directory, via the
    canonical content-root resolver and ``content_root_for`` (so a flat published
    mirror resolves as well as the private authoring tree, which is what a
    container registering the mirror gets). Returns ``None`` on any resolution
    failure (never raises) -- Checks 9/10 fail open on this, matching the
    bash originals' own "not a git repo at PLUGIN_ROOT" -> exit 2 -> no
    warning-appended fail-open shape (see this module's docstring)."""
    try:
        from coordinator_core.content_root import read_content_root
        from coordinator_core.data_root import content_root_for
    except Exception:
        return None
    try:
        base = read_content_root()
    except Exception:
        return None
    content_root = content_root_for(base)
    return str(content_root) if content_root is not None else None


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
    plugin_root = _resolve_plugin_content_root()
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
    plugin_root = _resolve_plugin_content_root()
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
    """Violation text for a staged settings.json (``rel_path``) carrying a
    machine-absolute leaf value, via ``machine_path_leak.leak_detail``; ``None``
    when clean or when the file cannot be read (fail open). Unparseable JSON is
    reported, not passed."""
    content = _read_settings_content(rel_path, cwd)
    if content is None:
        return None
    return machine_path_leak.leak_detail(rel_path, content)


# ---------------------------------------------------------------------------
# Check 12 -- registration-quad-completeness -- REGISTRATION-QUAD-INVARIANT
# (HARD block, override-gated). See module docstring above for the full
# resolution-mechanism writeup; this section is the implementation only.
# ---------------------------------------------------------------------------

# Anchored to the start of a (stripped) line so
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


def _same_tree(path_a: str, path_b: str) -> bool:
    return os.path.normcase(os.path.realpath(path_a)) == os.path.normcase(os.path.realpath(path_b))


def _index_reader(paths: List[str], cwd: Optional[str]) -> Callable[[str], Optional[bytes]]:
    """Reader over the post-commit tree: ONE ``git cat-file --batch`` over ``paths``
    resolves each ``:<path>`` index entry (the HEAD blob wherever nothing is staged).
    A path outside ``paths``, a missing entry, or a failed spawn reads as ``None``."""
    blobs: Dict[str, Optional[bytes]] = {p: None for p in paths}
    if paths:
        proc = run_git(
            ["cat-file", "--batch"],
            cwd=cwd,
            input=("\n".join(":" + p for p in paths) + "\n").encode("utf-8"),
        )
        if proc.returncode == 0:
            out = proc.stdout_bytes
            pos = 0
            for p in paths:
                nl = out.find(b"\n", pos)
                if nl == -1:
                    break
                header = out[pos:nl].split(b" ")
                pos = nl + 1
                if len(header) != 3 or header[1] != b"blob":
                    continue
                try:
                    size = int(header[2])
                except ValueError:
                    break
                blobs[p] = out[pos : pos + size]
                pos += size + 1
    return blobs.get


def check_registration_quad_completeness(
    cwd: Optional[str] = None, staged: Optional[List[str]] = None
) -> Optional[str]:
    """Hard-deny detail string when the tree this commit lands registers an op-key
    without all five quad surfaces, judged by
    ``registration_quad_static.registration_violations`` over an index reader.

    ``staged`` is the caller's already-resolved staged list; ``None`` reads it with
    one ``git diff --cached``. Returns ``None`` when clean, when no registration-relevant
    path is staged, or when the check cannot run (fails open). Judges the committing
    repo's own surface files, wherever ``coordinator_core`` was imported from.
    """
    try:
        from coordinator_core.authz.registration_quad_static import (
            _ALL_SURFACE_FILES,
            _SURFACE_PATHS,
            registration_violations,
        )

        if staged is None:
            rc, staged_out = _run_git(["diff", "--cached", "--name-only"], cwd=cwd)
            if rc != 0:
                return None
            staged = [l for l in staged_out.splitlines() if l]
        candidates = [f for f in staged if f.startswith("coordinator_core/") and f.endswith(".py")]
        if not candidates and not any(f in _SURFACE_PATHS for f in staged):
            return None
        read = _index_reader(sorted(set(candidates) | set(_ALL_SURFACE_FILES)), cwd)
        verdict = registration_violations(read, staged)
    except Exception:
        return None
    if verdict.outcome != "refuse" or not verdict.violations:
        return None  # an unreadable or ambiguous surface (no violations) fails open here
    relevant = verdict.violations

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
# Ruling + full empirical writeup: SC-DR-015, canonical text at coordinator-content-repo's
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
    if "--" in rest:
        last_dd = len(rest) - 1 - rest[::-1].index("--")
        paths = rest[last_dd + 1:]
        if not paths:
            return None
        return paths
    # `-o`/`--only <paths>` selects git's SAME index-bypassing self-scoped
    # mode with no `--` anywhere, so keying this extractor on the separator
    # made Check 13 structurally not-applicable to half the scoped-commit
    # surface. On the 2026-08-03 incident command shape that was the second
    # of two independent silences: the advisory was suppressed by a
    # directory operand, and this check never evaluated the command at all.
    # Spinoff: `state/handoffs/2026-08-03-commit-scope-guard-predicates.md`.
    #
    # The operand walk is DELEGATED, never re-implemented here. The import
    # is function-local because `dispatch_checks` imports this module at
    # its own import time; by the time any guard calls this, that module is
    # already in `sys.modules`, so this costs a dict lookup and no import
    # work on the PreToolUse hot path. Re-walking tokens locally would be a
    # third dialect of the same parse -- the exact two-independent-
    # recognizers defect the spinoff exists to remove.
    try:
        from coordinator_core.bash_guards.dispatch_checks import (
            _bt_commit_operand_scan,
        )
    except ImportError:
        return None
    operands, ambiguous, has_include, has_only = _bt_commit_operand_scan(tokens[i:])
    # Fail CLOSED to not-applicable, the posture this function already
    # documents above: an ambiguous parse, `--include` (which scopes
    # nothing), or `--only` naming no paths gives this check nothing it can
    # safely reason about.
    if ambiguous or has_include or not has_only or not operands:
        return None
    return operands


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


def _index_only_removed_paths(paths: List[str], cwd: Optional[str]) -> List[str]:
    """The subset of `paths` staged as an INDEX-ONLY removal (`git rm
    --cached`, or any staging that drops a path from the index while HEAD
    still carries it) whose worktree copy is still present on disk.

    This is a distinct divergence shape from `_diverging_paths`'s own: that
    predicate requires the path to still HAVE an index entry to reason about
    (`diverging_paths` excludes a path with no index entry as "not staged" --
    see that module's own `staged = [p for p in paths if relative[p] in
    index_snapshot]` gate). An untracked-from-the-index path never reaches
    that gate at all, so the deliberate "stop tracking this, keep the file"
    intent an operator expresses via `git rm --cached` was invisible to this
    check -- a trailing-pathspec `git commit -- <path>` would silently
    re-`add` the worktree content back into the index, reverting the untrack
    the operator just staged, with no warning of any kind.

    Detected via one `git diff --cached --name-status -- <paths>` scoped to
    the candidate paths (a `D` record with no accompanying `R`-shaped rename
    entry -- `--name-status` is not run with `-M` here, so a genuine rename
    reports as `D`+`A` on two different paths rather than one `R` record;
    this is fine, because both the D-side and A-side are members of `paths`
    only when the caller's own pathspec named them, and a rename's D-side
    target is gone from the worktree by construction, so `os.path.exists`
    below excludes it anyway). Returns `[]` on any git failure (fail open,
    matching every other predicate in this module)."""
    if not paths:
        return []
    rc, out = _run_git(["diff", "--cached", "--name-status", "--", *paths], cwd=cwd)
    if rc != 0:
        return []
    removed: List[str] = []
    for line in out.splitlines():
        if not line.startswith("D\t"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            removed.append(parts[1])
    still_on_disk: List[str] = []
    for rel in removed:
        abs_path = os.path.join(cwd, rel) if cwd else rel
        if os.path.exists(abs_path):
            still_on_disk.append(rel)
    return sorted(still_on_disk)


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

    # `fail_loud=True`, NOT the default: this check's answer decides whether the
    # operator is warned that their staged content is about to be discarded, and
    # `[]` conflates "no divergence" with "could not tell". The two are not
    # interchangeable HERE because the condition that makes the read fail --
    # another session holding `.git/index` -- is the same contention that causes
    # the discarding this check exists to catch. So the guard went silent exactly
    # when it was most needed. Measured at `IndexParseError` 2/200 under 12-way
    # concurrency on one repo, rising with concurrency, on a box specced for
    # 50-70 sessions.
    try:
        diverging = _diverging_paths(all_paths, cwd, fail_loud=True)
    except DivergenceCheckFailed:
        # Say so rather than warn about paths we cannot name. This check is an
        # advisory offer, so an indeterminate read must not become a block --
        # but it must not become silence either.
        return _INDETERMINATE_DIVERGENCE_OFFER.format(
            paths=", ".join(all_paths),
            override_note=operator_override_note(
                "COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE", payload=payload
            ),
        )

    untracked_removed = _index_only_removed_paths(all_paths, cwd)

    if not diverging and not untracked_removed:
        return None

    if _override("COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE"):
        _log_pathspec_divergence_override(cmd, cwd, session_id)
        return None

    if untracked_removed and not diverging:
        return _INDEX_ONLY_REMOVAL_OFFER.format(
            paths=", ".join(untracked_removed),
            override_note=operator_override_note(
                "COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE", payload=payload
            ),
        )

    untrack_note = ""
    if untracked_removed:
        untrack_note = (
            "\n\nOf these, {untrack_paths} are staged as an INDEX-ONLY removal "
            "(e.g. `git rm --cached`) whose worktree copy is still on disk -- a "
            "trailing pathspec here reads the worktree, so this commit would "
            "silently re-`add` that content back into the index and revert the "
            "untrack you just staged.\n\n"
        ).format(untrack_paths=", ".join(untracked_removed))

    paths_list = ", ".join(sorted(set(diverging) | set(untracked_removed)))
    return (
        "OFFER: this `git commit` has a trailing `--` pathspec covering {paths}, "
        "and the STAGED content there differs from the WORKTREE content -- the "
        "trailing pathspec reads the worktree, so this commit would silently "
        "discard what you staged there and substitute the worktree instead "
        "(SC-DR-015).{untrack_note}\n"
        "A bare no-pathspec commit is NOT the fix -- the shared index can gain a "
        "peer's staged file between your check and your commit (that TOCTOU has "
        "hit for real -- 7 files swept, including another "
        "session's in-flight agent definitions). Usually simplest: don't "
        "partial-stage on a shared "
        "tree at all -- make the worktree at {paths} match only your change, "
        "then commit normally. Staged an index-only removal you meant to keep? "
        "`git reset -q -- {paths}` restores those paths to HEAD's tracked state "
        "in the index without touching the worktree, so the pathspec commit no "
        "longer re-adds what you meant to untrack. Genuinely diverged and need "
        "to commit anyway? Isolate the commit in a private GIT_INDEX_FILE "
        "(write-tree + commit-tree) rather than the shared index (SC-DR-015).\n\n"
        "{override_note}"
    ).format(
        paths=paths_list,
        untrack_note=untrack_note,
        override_note=operator_override_note(
            "COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE", payload=payload
        ),
    )


# ---------------------------------------------------------------------------
# 14. UNDECLARED-STAGED-DELETION -- a commit that removes tracked files while
# its own message describes something else.
#
# The incident this is built against (state/bug-backlog/2026-08-31-four-bug-
# blitz-commits-deleted-five-file-6216c89502b9.yaml, P0): four commits from a
# single bug-blitz run each landed `0 insertions, N deletions` under a subject
# describing a fix that was nowhere in its own diff -- one of them deleted
# `coordinator_core/authz/classification.py` entire (4043 lines) under the
# subject "git.maintenance and handoff.repair_deployment_state reach
# OP_CLASSIFICATION". Root cause was a cwd-dependent existence probe in
# `coordinator-safe-commit :: do_pathspec` turning a NEGATIVE EXISTENCE PROBE
# into a POSITIVE DELETION DECLARATION.
#
# WHY NOTHING CAUGHT IT, and why this has to be a commit-shape check rather
# than a test: the working tree keeps functioning perfectly afterwards.
# Imports resolve, tests pass, guards go green -- all against a copy git does
# not have. There is no failing signal to notice. The blast radius is a fresh
# clone, a publish, or one `git clean` in a tree ~50 peer sessions share, and
# the failure then surfaces far from its cause. A test suite structurally
# cannot catch it: it imports from disk.
#
# PREDICATE, and it is deliberately NOT the one that row's body first
# proposed. That row proposed `insertions == 0 AND deletions > 0`; its own
# later REFUTED block measured that predicate wrong over all 31,983 commits
# and named two counter-examples where the deletion RODE ALONG inside an
# otherwise normal commit carrying insertions (`d721e7b3e1` deleting a
# doctor test, `e3f53f3d6c` sweeping six `state/recovery/**.py`). An
# `insertions == 0` gate misses both. So this keys on the presence of a
# staged deletion at all, never on the insertion count.
#
# The second half of the predicate is the deletion-verb scan, and it reads
# the WHOLE commit message rather than the subject line alone -- also a
# correction to the row, which proposed a subject-only scan. Measured here
# over this branch's 699 reachable commits: 11 carry a staged `D`, and a
# subject-only scan fires on 8 of them, all 8 legitimate (`Untrack ...`,
# `untrack ...`, `discard the staged draft`, `quarantine ...` -- each
# declaring the removal in a word the subject-only verb list did not hold, or
# declaring it one line further down in the body). Reading the full message
# with the verb list below takes that to 0 false positives over the same 699.
#
# RENAMES ARE NOT DELETIONS. The status probe is run with `-M`, so a
# `git mv` -- which is how every queue closure in this codebase archives a
# row -- reports `R`, not `D`, and never reaches this check. That matters
# directly: the archive path is the single highest-volume producer of staged
# removals in this repo, and a guard that fired on it would be turned off
# within a day.
#
# NOT VERIFIED, and stated rather than papered over: the four accident
# commits, and the three rides-along victims the REFUTED block names, are
# unreachable from this branch (shallow clone, 699 commits) -- so the
# false-positive rate above is measured and the true-positive rate is
# REASONED, from those commits' recorded subjects, not re-measured. Anyone
# with the full history should re-run the measurement in
# `test_undeclared_staged_deletion.py`'s docstring against it.
#
# POSTURE: advisory, not a deny. `check_registration_quad_completeness`
# (Check 12) denies because an incomplete quad is statically decidable from
# the staged diff alone. This check is not in that position: it cannot read a
# message passed by `-F` or composed in an editor, so it fails open on a real
# fraction of commits, and a guard that blocks the shapes it can read while
# waving through the ones it cannot would buy compliance rather than safety.
# The advisory names the paths, which is the thing the operator could not
# otherwise see -- the deletion is invisible in the command they typed.

_DELETION_VERBS = re.compile(
    r"\b("
    r"delet\w*|remov\w*|rm|retir\w*|drop(s|ped|ping)?|gravestone\w*|"
    r"prun\w*|purg\w*|kill(s|ed|ing)?|untrack\w*|discard\w*|quarantin\w*|"
    r"obsolet\w*|sunset\w*|revert\w*|supersed\w*|retract\w*|withdraw\w*|"
    r"mov(e|es|ed|ing)|mv|renam\w*|relocat\w*|archiv\w*|migrat\w*|"
    r"clean(s|ed|up)?|strip(s|ped|ping)?|excis\w*|unregister\w*"
    r")\b",
    re.IGNORECASE,
)

# Options that take a separate value argument, so the token after them is
# never itself a message.
_COMMIT_OPTS_WITH_VALUE = {
    "-C", "--reuse-message", "-c", "--reedit-message",
    "-F", "--file", "--author", "--date", "--cleanup",
    "--gpg-sign", "-S", "--pathspec-from-file", "--fixup", "--squash",
    "-t", "--template",
}

# Options that mean "the message does not come from this command line".
_COMMIT_OPTS_MESSAGE_ELSEWHERE = {
    "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
    "-t", "--template", "--fixup", "--squash",
}


def _commit_message_from_tokens(seg_tokens: List[str]) -> Optional[str]:
    """Concatenate the ``-m``/``--message`` values in a tokenized ``git
    commit`` segment, or ``None`` when the message is NOT statically knowable
    from the command line.

    ``None`` is returned -- and callers must fail open on it, never treat it
    as an empty message -- for ``-F``/``--file``, ``-C``/``-c``,
    ``--template``, ``--fixup``/``--squash``, and for a bare ``git commit``
    with no ``-m`` at all (the message is composed in an editor this process
    never sees). Reading those as "no deletion verb present" would fire the
    advisory on every editor-composed commit in the repo.
    """
    parts: List[str] = []
    i = 0
    saw_message = False
    while i < len(seg_tokens):
        tok = seg_tokens[i]
        if tok == "--":
            break
        if tok in _COMMIT_OPTS_MESSAGE_ELSEWHERE:
            return None
        if tok in ("-m", "--message"):
            if i + 1 < len(seg_tokens):
                parts.append(seg_tokens[i + 1])
                saw_message = True
                i += 2
                continue
            return None
        if tok.startswith("--message="):
            parts.append(tok.split("=", 1)[1])
            saw_message = True
            i += 1
            continue
        if tok.startswith("-m") and len(tok) > 2 and not tok.startswith("--"):
            # Attached short-option form: `-mSubject`.
            parts.append(tok[2:])
            saw_message = True
            i += 1
            continue
        if tok in _COMMIT_OPTS_WITH_VALUE:
            i += 2
            continue
        i += 1
    if not saw_message:
        return None
    return "\n\n".join(parts)


def _staged_deletions(status_lines: List[str]) -> List[str]:
    """Paths reported ``D`` by a ``git diff --cached --name-status -M`` run.

    Rename records (``R100\told\tnew``) are three-field and start with ``R``,
    so they are skipped here without any extra branch -- which is the whole
    reason the caller must pass ``-M``. A ``D`` line is two fields.
    """
    out: List[str] = []
    for line in status_lines:
        if not line or not line.startswith("D\t"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            out.append(parts[1])
    return out


def check_undeclared_staged_deletion(
    commit_seg_tokens: Optional[List[str]],
    status_lines: List[str],
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Advisory detail string when this commit stages one or more file
    DELETIONS and its own message never says so; ``None`` otherwise.

    Takes ``status_lines`` (the caller's already-fetched
    ``git diff --cached --name-status -M`` output) rather than running its
    own probe, so this check costs ZERO additional processes: adding it
    actually REMOVED one from the pathspec'd commit path, because
    ``check_validate_commit`` was running a ``--name-only`` probe and a
    second ``--name-status`` probe that are now one call. That is not
    incidental tidiness -- this runs on the commit hot path, where the
    brightline budget is 500ms end-to-end and process creation, not the
    query, is the cost (DR-344).

    Fails open (``None``) when the message is not on the command line -- see
    ``_commit_message_from_tokens``.
    """
    if commit_seg_tokens is None:
        return None

    deletions = _staged_deletions(status_lines)
    if not deletions:
        return None

    message = _commit_message_from_tokens(commit_seg_tokens)
    if message is None:
        return None

    if _DELETION_VERBS.search(message):
        return None

    shown = deletions[:10]
    more = len(deletions) - len(shown)
    listed = "\n".join("  D  %s" % p for p in shown)
    if more > 0:
        listed += "\n  ... and %d more" % more

    return (
        "UNDECLARED STAGED DELETION: this commit removes %d tracked file(s), "
        "and its message does not mention a removal.\n\n"
        "%s\n\n"
        "If that is intended, say so in the message and this stops firing. If "
        "it is not, the deletion is almost certainly a pathspec that did not "
        "match your intent -- `git restore --staged <path>` puts it back "
        "before the commit lands.\n\n"
        "Why this is worth a look: the working tree keeps working either way. "
        "A file deleted from git but still present on disk breaks nothing "
        "here -- it breaks the next fresh clone, publish, or `git clean`, far "
        "from this commit.\n\n"
        "%s"
    ) % (
        len(deletions),
        listed,
        operator_override_note(
            "COORDINATOR_OVERRIDE_UNDECLARED_DELETION", payload=payload
        ),
    )


# ---------------------------------------------------------------------------
# 15. TRAILER-DEMOTED-TO-BODY -- a trailer-shaped line sitting one paragraph
# above the commit's real trailer block, silently read as body prose.
#
# The incident this guards (state/bug-backlog/2026-09-19-a-blank-line-turns-
# a-git-trailer-into-bo-964db9e54ea6.yaml, P1): three chunk commits on
# 2026-08-21 were written with successive `-m` flags --
#
#     -m "..." -m "Deliverable-Id: dlv-..." -m "Co-Authored-By: Claude Opus 5 <...>"
#
# -- each `-m` becoming its own blank-line-separated paragraph. `git` parses
# ONLY the message's final paragraph as trailers (see
# `coordinator_core.git.commit_trailers._extract_trailer_block`'s own
# docstring for the same rule, verified against real `git interpret-
# trailers`), so `Deliverable-Id` landed as body text -- present to a human
# reading `git log`, invisible to `git log --format=%(trailers:...)` and
# every consumer that reads trailers that way. `close-out-and-stamp` reported
# all eight chunk ids missing over a range that provably contained every one
# of them.
#
# PREDICATE: every paragraph of the message except the LAST is checked with
# `_paragraph_is_trailer_shaped` (the same per-line `Token: value`-or-
# continuation regex `commit_trailers.py` already defines and this module
# reuses rather than re-derives). Any hit is a trailer-shaped paragraph git
# will never parse as one, because a blank line separates it from the
# paragraph git actually reads.
#
# This is deliberately NOT bounded by `_trailing_region_lines` (the WIDER
# reading `extract_closure_facts` uses to stay robust to an `-m`-built
# message when reading a message THIS engine already composed): widening the
# boundary here would absorb the exact demoted paragraph the incident
# depends on back into the "recognized" region and the guard would never
# fire on its own reference shape -- `_extract_trailer_block`'s single-last-
# paragraph reading is the one that matches what `git log --format=%(trailers:
# ...)` actually does, and that is the question this check answers.
#
# POSTURE: advisory only, matching Checks 13/14's posture, for the same
# reason -- this reads only the commit-line-visible message
# (`_commit_message_from_tokens` fails open on `-F`/`-C`/editor-composed
# forms) and a real, if narrower, false-positive surface exists (a
# deliberately quoted prior commit message in the body, colon-shaped
# metadata prose). Never denies.
# ---------------------------------------------------------------------------


def check_trailer_demoted_to_body(
    commit_seg_tokens: Optional[List[str]],
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Advisory detail string when this commit's own message carries a
    trailer-shaped paragraph (per ``_paragraph_is_trailer_shaped``) that is
    neither the message's FIRST paragraph (the subject -- git always reads
    the first paragraph as the subject regardless of its shape, so a subject
    written `prefix: description` style, this repo's own convention, must
    never be mistaken for a demoted trailer) nor its LAST (the paragraph git
    actually parses as the trailer block). The blank line on either side of
    such a paragraph demotes it to body prose, invisible to
    ``git log --format='%(trailers:...)'`` and everything that reads
    trailers that way (see module comment block above for the recorded
    incident). ``None`` when the message is not knowable from the command
    line (see ``_commit_message_from_tokens``), when it has fewer than three
    paragraphs (nothing sits strictly between a subject and a trailing
    block), or when every paragraph in between is ordinary prose.
    """
    if commit_seg_tokens is None:
        return None

    message = _commit_message_from_tokens(commit_seg_tokens)
    if message is None:
        return None

    paragraphs = _ct_split_paragraphs(message)
    if len(paragraphs) < 3:
        return None

    demoted = [
        p for p in paragraphs[1:-1] if _ct_paragraph_is_trailer_shaped(p)
    ]
    if not demoted:
        return None

    listed = "\n".join(
        "  %s" % line for paragraph in demoted for line in paragraph
    )

    return (
        "TRAILER DEMOTED TO BODY: this commit message has a trailer-shaped "
        "line separated from the message's final paragraph by a blank line. "
        "`git` parses ONLY the last paragraph as trailers, so the line(s) "
        "below will land as ordinary body prose -- readable to a human, "
        "invisible to `git log --format=%%(trailers:...)` and everything "
        "that reads trailers that way.\n\n"
        "%s\n\n"
        "Fix: join it into the same trailing paragraph as the rest of the "
        "trailers -- drop the extra `-m` (or the blank line) that separates "
        "them.\n\n"
        "%s"
    ) % (
        listed,
        operator_override_note(
            "COORDINATOR_OVERRIDE_TRAILER_DEMOTED_TO_BODY", payload=payload
        ),
    )
