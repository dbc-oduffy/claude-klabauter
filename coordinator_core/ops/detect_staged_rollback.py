"""
coordinator_core.ops.detect_staged_rollback — staged mass-deletion tripwire.

Purpose: reads the caller's staged index (``git diff --cached``) and flags a
staged mass deletion (added 2026-08-10, see
``state/bug-backlog/2026-08-10-nothing-on-the-commit-path-can-see-a-mas-486778a10476.yaml``)
— the shape the 2026-07-28 incident's root cause actually produced: commit
``0a3462b72`` staged git's canonical empty tree
(``4b825dc642cb6eb9a060e54bf8d69288fbee4904``) against a parent holding
18,506 files. See ``find_mass_deletion`` / ``MASS_DELETION_RATIO_THRESHOLD`` /
``MASS_DELETION_ABS_FLOOR`` below for the detection logic and thresholds.

KILLED CHECK (2026-08-21, PM ruling): this module used to also run an
exact-blob rollback detector (the module's ORIGINAL purpose — staged blobs
byte-identical to an older commit's blob for the same path). That check was
deleted, not merely disabled: 11,380ms of process time on every commit
(~2.66h/day at 840 commits/day, 98% of a commit) for a signal that fired
correctly on roughly 1-in-22 of its real firings and was, by its own
``_staged_blobs``'s by-design status-``D`` skip, BLIND to the very incident
(``0a3462b72``) that motivated this module. The ruling was made on the
mechanism (cost per inspection, and blindness to the founding incident), not
on an absence of evidence — the gate DID make someone look, at least once
(see ``70ed82e4aa6f``, ``e167d08d14ad``, ``0f37a6fac346``) — and not on the
cross-session-clobber hazard being unreal (it is real; it is simply not this
mechanism's to guard). See
``docs/plans/2026-08-21-the-cli-bootstrap-tax-dies-at-the-interpreter-floor.md``
chunk C16 for the full ruling and the shapes considered and refused
(off-path move, a standing DR-344 exception) before landing on deletion.

This module never mutates a repo — it is read-and-report only. It IS wired
into a commit path: it is the sole entry in
``coordinator_core.ops.install_claude_klabauter_precommit_hook._GATE_REGISTRY``, the
registry that installs this repo's own ``.git/hooks/pre-commit`` (via the
``coordinator/bin/detect-staged-rollback.py`` trampoline). This remains the
ONLY gate that fires on this repo's commit path — every commit into
Claude-klabauter runs through ``main()`` here before it can land.

Detection logic (mass deletion):
    Reads every staged status-``D`` path and the tracked-file total at
    ``HEAD`` (the pre-commit tree). Fires when EITHER the absolute
    deleted-path count crosses ``MASS_DELETION_ABS_FLOOR`` OR the
    deleted-path count is at least ``MASS_DELETION_RATIO_THRESHOLD`` of the
    tracked total — an OR, not an AND, because either signal alone is
    sufficient evidence of an implausible deletion: a huge repo losing a
    proportionally-small-but-absolutely-enormous subtree should fire on the
    floor even if the ratio stays low, and a small repo (or subtree) losing
    nearly everything should fire on the ratio even if the absolute count is
    unremarkable. See MASS_DELETION_ABS_FLOOR / MASS_DELETION_RATIO_THRESHOLD
    docstrings for the numbers and how they were derived from this repo's own
    commit history.

Override (deliberate divergence from a no-override discipline): unlike a
correctness guard, this check has a real and benign false-positive case — a
DELIBERATE large prune is legitimate work indistinguishable, from git alone,
from the incident this check exists to catch. So this module takes one named
override env var, spelled to match the sibling precommit gates' convention
in ``coordinator_core.ops.install_meta_repo_precommit_hook._GATE_REGISTRY``:
``COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION``. Do NOT "fix" this into a
hard, unoverridable block by analogy with a correctness guard — every one of
a correctness guard's refusal cases is a genuine defect; this is not, by
construction. A later reader who removes the override on that analogy would
be reintroducing the exact false-positive this check was built to tolerate.
Per the 2026-08-10 bug-backlog ruling, the override is biased toward NOT
firing on plausible legitimate work — this repo runs ~50 concurrent sessions
sharing one hook, and a false positive here blocks every one of them from
committing. Missing a marginal real incident is the deliberately preferred
failure mode over blocking legitimate work; see the threshold docstrings for
the historical margin it keeps.

That bias governs the THRESHOLDS, not the spelling of the key. The override
arms on exactly ``"1"`` and on nothing else (``_override_armed``, PM-authorized
2026-08-21): the earlier "anything but empty-or-0" form silently disarmed the
tripwire for the operator who typed ``=false`` meaning the opposite, and
disagreed with the generated hook's own ``[ "$KEY" = "1" ]``. Keeping the
override cheap to reach is the bias; guessing which string means yes is not
part of it.

Negative-spec:
    - NEVER runs a mutating git command (no ``git reset``, ``git checkout``,
      no staging/unstaging) — read-only over ``git diff --cached`` / ``git
      rev-parse`` / ``git ls-tree`` only.
    - Does NOT weight staged deletions by path — a deletion of ``README.md``
      and a deletion of a 40MB generated artifact count identically as "one
      path". Byte-weighting is a different, unbuilt detector; this one
      answers "how much of the TREE, by path count, is gone", which is what
      an empty-tree-shaped incident actually produces.
    - Does NOT accumulate deletions across commits — each invocation
      measures only the CURRENT staged commit's deleted-path count/ratio
      against the tracked total at that moment; nothing here tracks a
      running total across a SEQUENCE of commits. A paced split (e.g. ~900
      deletions per commit across 20 commits on a repo whose floor is 5127)
      can keep both the absolute-floor and ratio legs under threshold on
      every single commit while still emptying the tree over the sequence —
      the exact broader-gap shape the carried backlog item
      ``cf-deletion-tripwire-3a91c7`` names. EXECUTED AND CONFIRMED
      2026-08-11, no longer a reasoned-about gap —
      ``docs/research/spike-verdicts/2026-08-11-paced-deletion-evades-the-mass-deletion-tripwire.md``
      ran these predicates unmodified at production thresholds against a
      6000-file repo deleting HALF the remainder per commit: the guard stayed
      silent for 13 consecutive commits and first fired at commit 14, with
      5999 of 6000 files already gone. That spike also establishes a stronger
      claim than "this is a gap": NO single-commit threshold can close it.
      The 0.90 ratio exists because this repo's largest legitimate bulk
      deletion measured 0.505, so any threshold above that is evaded by
      pacing just under it, and any threshold at or below it blocks a
      deletion this repo's own history calls legitimate. A cross-commit
      window is the only shape that can work. This is an accepted, undisclosed
      (until now) gap: closing it needs a bounded rolling-window check (sum
      of staged-D counts across the last N pre-commit invocations), which is
      a distinct, unbuilt detector, not a fix to the per-commit measurement
      this module makes.
    - Does NOT scope the mass-deletion check to the pathspec a
      `git commit -- <path>` will actually write — it reads the FULL staged
      index via an unscoped ``git diff --cached --raw``, not the subset of
      the index a pathspec-scoped commit will turn into tree content (git
      uses HEAD content for unlisted paths in that mode). A developer with
      unrelated deletions staged elsewhere who runs a narrow
      `git commit -- some/file` is evaluated against the WHOLE staged set.
      This is NOT a fixable oversight in this module: a standard git
      pre-commit hook is invoked with no arguments and no environment
      variable carrying the commit's pathspec (confirmed by reading
      ``coordinator_core.ops.install_claude_klabauter_precommit_hook`` and
      ``coordinator/bin/detect-staged-rollback.py`` — both forward/receive
      only ``[repo-root]``, never the invoking `git commit`'s own argv) — a
      pre-commit hook has no git-provided way to see which pathspec the
      commit that triggered it will restrict itself to. Named here as a
      git-hook-API limitation, not silently accepted.
    - Does NOT itself decide whether to block anything beyond its own exit
      code — it is a read/report library plus a CLI; the installed hook
      (``coordinator_core.ops.install_claude_klabauter_precommit_hook``) clamps
      whatever nonzero code this module returns to exactly 1 at the commit
      boundary (see that module's own docstring, "Exit-code clamping").
"""

from __future__ import annotations

import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# Cross-package import of the SSOT doc-pointer display string -- the same
# precedent `write_guards` already uses for `operator_override_note` itself
# (see docs/reference/guard-override-keys.md's "Override keys, by guard"
# section). Never hand-copy the "claude-klabauter <path>" string here; both
# checks' remediation text below must point readers at the one doc that
# actually enumerates these two keys, not name them inline (B6/B8, see
# docs/wiki/guard-messaging.md § Register).
#
# Imported from the leaf `_override_doc` module, not `_helpers`
# (2026-08-25, "the commit gate stops importing a subsystem"): this module
# sits on the commit pre-commit hook path, so importing it must not pull in
# `_helpers.py`'s wider `bash_guards`/`subagent_sandbox` import graph for a
# module-level constant that never needed any of it.
from coordinator_core.bash_guards._override_doc import OVERRIDE_KEYS_DOC_DISPLAY

# --- Exit-code contract ---------------------------------------------------
#
# With only one check left (check 1, the exact-blob rollback detector, was
# deleted 2026-08-21 — see module docstring), the exit contract collapses
# back to the ordinary 0/1/2 shape: 0 clean, 1 a real finding (unresolved by
# its override), 2 a usage error. There is no longer any ambiguity for a
# caller to resolve between two checks, so there is nothing for a wider code
# to disambiguate.
EXIT_CLEAN = 0
EXIT_MASS_DELETION_FINDING = 1
EXIT_USAGE_ERROR = 2

# --- Mass-deletion tripwire (2026-08-10) ---------------------------------
#
# Both constants below were derived from this repo's OWN commit history, not
# picked as round numbers — see
# state/bug-backlog/2026-08-10-nothing-on-the-commit-path-can-see-a-mas-486778a10476.yaml
# for the incident this exists to catch (commit 0a3462b72, which staged
# git's canonical empty tree against an 18,506-file parent — every one of
# those 18,506 paths a status-D entry, deleted_count/tracked_total == 1.0).
#
# A full sweep of every commit in this repo's history (`git log --all
# --name-status`, ~13,200 commits) for its per-commit status-D count, cross-
# referenced against the tracked-file total at that commit's PARENT (the
# pre-commit tree, matching what this module measures at commit time), found
# exactly one commit at ratio 1.0 (the incident itself, 18,506/18,506) and a
# clear runner-up at ratio 0.505 (1,709/3,382 — `e6783a68bd0`, "prune(reclaim):
# drop 1,709 pre-July example-doctrine-mirror-repo files reclaimed by DoE", a deliberate,
# legitimate bulk prune). Every other commit with >=15 deletions in the same
# sweep sits at ratio <= 0.102. There is no commit anywhere between 0.505 and
# 1.0 — the two shapes (largest legitimate prune vs. the incident) are not
# close together, which is what makes a threshold between them defensible
# rather than arbitrary.
MASS_DELETION_RATIO_THRESHOLD = 0.90
"""Fire when staged deletions are >= this fraction of the tracked total at
HEAD. 0.90 sits with ~1.8x headroom above the largest legitimate single-commit
deletion ratio ever observed in this repo (0.505) while leaving 0.10 of
margin below the incident's 1.0 — i.e. up to 10% of the tree could survive a
staged commit and this would still fire, but a commit leaving MORE than 10%
of the tree intact (as every real prune in this repo's history has) does not.
Per the ~50-concurrent-session load constraint, the margin above the
historical legitimate max (0.505) is deliberately generous rather than tight
against it — a future prune materially larger than `e6783a68bd0` (but still a
genuine partial prune, not a total wipe) must not trip this."""

MASS_DELETION_ABS_FLOOR = 5127
"""Fire when the ABSOLUTE staged-deletion count crosses this, regardless of
ratio (catches a huge deletion inside a huge repo that stays proportionally
small). Derived as 3x the largest legitimate single-commit deletion COUNT
ever observed in this repo's history (1,709, the same `e6783a68bd0` commit
the ratio threshold is keyed to) — a 3x safety margin above the historical
worst case, biased toward the same "miss a marginal case over blocking
legitimate work" preference the ratio threshold uses. This floor is a
belt-and-braces catch for a repo shape where the ratio leg could stay under
threshold (e.g. a 200,000-file monorepo losing 10,000 files is only a 5%
ratio, but 10,000 deleted files is not a plausible ordinary edit) — the two
legs are OR'd together (`_mass_deletion_should_fire`), not AND'd, precisely
so neither shape has to also satisfy the other to be caught."""

MASS_DELETION_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION"

_PROG = "detect-staged-rollback"


def _run_git(args: Sequence[str], cwd: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )


def _staged_raw_diff(repo_root: str, env: Optional[dict] = None) -> List[str]:
    """``git diff --cached --raw -z --no-renames --no-abbrev`` spawn,
    tokenized on NUL with the trailing empty token trimmed.

    `_staged_deletions` needs this staged-index raw-diff scan. Deliberately
    returns the bare token list, not a parsed dict/list, so the caller's own
    parsing does not have to change to use it.
    """
    result = _run_git(
        ["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev"],
        cwd=repo_root,
        env=env,
    )
    if result.returncode != 0:
        return []
    tokens = result.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens = tokens[:-1]
    return tokens


@dataclass(frozen=True)
class MassDeletionFinding:
    deleted_count: int
    tracked_total: int
    ratio: float  # 0.0 when tracked_total is 0 (nothing to divide by)
    deleted_paths: Tuple[str, ...] = field(default_factory=tuple)


def _staged_deletions(
    repo_root: str,
    env: Optional[dict] = None,
    tokens: Optional[List[str]] = None,
) -> List[str]:
    """Every staged status-D path.

    Same single `git diff --cached --raw -z --no-renames --no-abbrev` shape
    as `_staged_raw_diff` produces. Pass a pre-fetched `tokens` (as `main()`
    does) to avoid a second `git diff --cached` process per commit; omit it
    to have this function fetch its own (unchanged standalone behavior).
    """
    if tokens is None:
        tokens = _staged_raw_diff(repo_root, env=env)

    deleted: List[str] = []
    i = 0
    while i + 1 < len(tokens):
        rawline = tokens[i]
        path = tokens[i + 1]
        i += 2
        if not rawline.startswith(":"):
            continue
        parts = rawline[1:].split(" ")
        if len(parts) != 5:
            continue
        _old_mode, _new_mode, _old_sha, _new_sha, status = parts
        if status.startswith("D"):
            deleted.append(path)
    return deleted


def _tracked_file_count(repo_root: str, env: Optional[dict] = None) -> int:
    """Tracked-file total at HEAD — the pre-commit tree the staged deletions
    are being measured against. 0 on any failure (no HEAD yet, e.g. a repo's
    first commit — nothing can be a "mass deletion" of a tree that does not
    exist yet; the ratio leg is skipped in that case and only the absolute
    floor can fire, which is correct: an initial commit cannot delete
    anything that was ever tracked)."""
    result = _run_git(
        ["ls-tree", "-r", "--name-only", "-z", "HEAD"],
        cwd=repo_root,
        env=env,
    )
    if result.returncode != 0:
        return 0
    tokens = result.stdout.split("\0")
    return sum(1 for t in tokens if t)


def find_mass_deletion(
    repo_root: str, env: Optional[dict] = None, tokens: Optional[List[str]] = None
) -> Optional[MassDeletionFinding]:
    """Read-only scan of the staged index for an implausible-share deletion.

    Never mutates the repo. Returns None when nothing is staged for
    deletion — a `MassDeletionFinding` otherwise, regardless of whether it
    crosses either threshold (`_mass_deletion_should_fire` makes that call);
    this function is measurement only, kept separate from the fire decision.

    `tokens`, if given (as `main()` does), is a pre-fetched `_staged_raw_diff`
    result forwarded straight to `_staged_deletions` to avoid a duplicate
    spawn; omit it to have `_staged_deletions` fetch its own (unchanged
    standalone behavior).
    """
    deleted = _staged_deletions(repo_root, env=env, tokens=tokens)
    if not deleted:
        return None
    total = _tracked_file_count(repo_root, env=env)
    ratio = (len(deleted) / total) if total else 0.0
    return MassDeletionFinding(
        deleted_count=len(deleted),
        tracked_total=total,
        ratio=ratio,
        deleted_paths=tuple(deleted),
    )


def _mass_deletion_should_fire(finding: Optional[MassDeletionFinding]) -> bool:
    if finding is None:
        return False
    if finding.deleted_count >= MASS_DELETION_ABS_FLOOR:
        return True
    return finding.tracked_total > 0 and finding.ratio >= MASS_DELETION_RATIO_THRESHOLD


def _mass_deletion_report(finding: MassDeletionFinding, overridden: bool) -> str:
    verb = "would flag" if overridden else "BLOCKED"
    pct = f"{finding.ratio * 100:.1f}%" if finding.tracked_total else "n/a (no HEAD)"
    lines = [
        f"{_PROG}: {verb} — staged commit deletes {finding.deleted_count} of "
        f"{finding.tracked_total} tracked path(s) ({pct}).",
    ]
    sample = sorted(finding.deleted_paths)[:10]
    for p in sample:
        lines.append(f"  D  {p}")
    remaining = finding.deleted_count - len(sample)
    if remaining > 0:
        lines.append(f"  ... and {remaining} more")
    if overridden:
        lines.append(
            f"{_PROG}: override is set — proceeding despite the above (deliberate mass deletion)."
        )
    else:
        lines.append(
            f"{_PROG}: if this IS a deliberate mass deletion (archival sweep, bulk prune), "
            f"see {OVERRIDE_KEYS_DOC_DISPLAY} for override options."
        )
    return "\n".join(lines)


def _override_armed(env: dict, key: str) -> bool:
    """Is `key` set to the one value that arms an override — exactly ``"1"``?

    Both overrides here disarm a tripwire, so the comparator decides which way
    an ambiguous value fails. The prior form (`not in ("", "0")`) armed on
    ANYTHING else, which inverts the intent of the most likely thing an
    operator types after a key spelled like a boolean: `=false`, `=no`, and
    `=off` all read as "leave the tripwire on" and all disarmed it, silently.
    The generated pre-commit hook already tested `[ "$KEY" = "1" ]` on its own
    side, so strictness here also retires a comparator mismatch that only ever
    resolved by accident.

    This narrows an override the module docstring records as deliberately
    permissive; the bias it names is toward not BLOCKING legitimate work, and
    an operator who meant to disarm still can, by the value the hook and this
    CLI now both name. PM-authorized 2026-08-21.
    """
    return env.get(key, "") == "1"


_USAGE = """\
usage: detect-staged-rollback [repo-root]

Read-and-report detector for a staged deletion covering an implausible share
of the tracked tree. Never mutates a repo. repo-root defaults to the current
working directory.

exit codes:
  0  clean (no finding, below threshold, or the override is set)
  1  a mass-deletion finding crossed the ratio/floor threshold, unresolved by
     COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION
  2  usage error

overrides:
  see {doc} for this CLI's override keys
""".format(doc=OVERRIDE_KEYS_DOC_DISPLAY)


def main(argv: Optional[List[str]] = None, env: Optional[dict] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    env = dict(os.environ) if env is None else dict(env)

    # A leading-dash arg is never a repo root. Without this, `--help` was
    # taken as a path and the CLI died on `FileNotFoundError: '--help'` — a
    # traceback where a usage block belongs. Hand-rolled rather than argparse
    # so exit 2 stays THIS module's usage-error code and does not collide with
    # the trampoline's own transport-failure 2 by accident of argparse's
    # convention (see coordinator/bin/detect-staged-rollback.py's docstring,
    # which enumerates both).
    if argv and argv[0] in ("-h", "--help"):
        print(_USAGE, end="")
        return 0
    if argv and argv[0].startswith("-"):
        print(f"detect-staged-rollback: unknown option {argv[0]!r}\n", file=sys.stderr)
        print(_USAGE, end="", file=sys.stderr)
        return 2

    repo_root = argv[0] if argv else "."

    raw_diff_tokens = _staged_raw_diff(repo_root, env=env)

    mass_finding = find_mass_deletion(repo_root, env=env, tokens=raw_diff_tokens)
    mass_fires = _mass_deletion_should_fire(mass_finding)
    if not mass_fires:
        return EXIT_CLEAN

    assert mass_finding is not None  # _mass_deletion_should_fire(None) is False
    mass_overridden = _override_armed(env, MASS_DELETION_OVERRIDE_ENV)
    print(_mass_deletion_report(mass_finding, mass_overridden), file=sys.stderr)

    if mass_overridden:
        return EXIT_CLEAN
    return EXIT_MASS_DELETION_FINDING


if __name__ == "__main__":
    sys.exit(main())
