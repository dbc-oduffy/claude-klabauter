"""
coordinator_core.ops.emit.resolvers — run-context resolution, root resolvers, and the
git-ancestor helpers. NOT an emitter: nothing here assembles or writes an artifact.

What this module was: the top-level snapshot assembly and output write for
`state/cockpit-emission.json` — a section registry, twenty-one porters, a post-collect
enrichment stack, and `emit()`.

What happened to it: the emission artifact was CUT on 2026-08-22 (PM ruling; see
`docs/problems/2026-08-22-artifact-emit-cannot-be-earned-back-in-its-current-shape.md`).
`build()`, `emit()`, `_empty_skeleton`, the registry (`register_section` / `clear_registry` /
`_wire_sections` / `SECMAP` / `_REGISTRY`) and every `_stamp_*` post-collect enrichment were
removed on 2026-08-23, verified unreachable from any production entry point.

What survives, and why — each has a live caller outside this package:

    resolve_context               the three `query-*` CLIs, `goal_append`, `goal_close_day`,
                                  `workday_complete.brief`
    main` / `_CHECK_SHIPPED_HELP  the `check-shipped-on-main` port (see the note below)
    sha_on_origin_main,           `ops/introspect/verify_shipped.py`
    classify_shas_on_origin_main,
    check_origin_main_reachable,
    resolve_ref
    resolve_coordinator_root,     root resolution for the above
    _resolve_central_state_root,
    _registry_coordinator_root
    DEFAULT_OUTPUT_NAME           `ops/emit/skipped_stage.py` (in-package; its only
                                  out-of-package consumer, tracker_tier_a.py, was retired)

The twenty-one section porters under `sections/` were deliberately NOT removed. Seven have
live callers behind the `query-*` surface (goals, rollups, routine_signals, trackers,
initiatives, review_trail, handoff_columns) and `commit_closures` is the source for a query
surface currently being built for example-cockpit-repo. The rest are producer libraries with no
aggregate caller: the artifact was the defect, not the producers, and cockpit's
section-by-section migration is what consumes them next. Do not delete one because
`build()` no longer calls it — check for a `query-*` consumer first.

One loose end, named rather than silently left: `sections/initiatives.py` still stages a
transient `_goal_ids` key that `_stamp_initiative_goals` used to pop. Nothing pops it now.
It is harmless (the section's own docstring documents it as ignorable) and a future
initiatives query surface will want the parse, so it stays until that surface claims it.

Dual-purpose note (deliberate, not incidental): this module also hosts the
``check-shipped-on-main.sh`` port (``main()``, ``_CHECK_SHIPPED_HELP``, ``resolve_ref``
below) because it reuses ``sha_on_origin_main`` / ``check_origin_main_reachable``. Those
helpers were originally shared with ``_stamp_shipped_sha``'s envelope assembly; with the
writer gone, the CLI and ``verify_shipped.py`` are their only consumers, and this module is
now named for a pipeline it no longer runs. Renaming it is a follow-on with an import-site
cost in eleven files, not a cleanup to fold in here.

Public promotion (2026-07-25): the ancestor-check helpers above were promoted from
private to public names so ``coordinator_core/ops/introspect/verify_shipped.py`` can import
them as a second, cross-module consumer, rather than reaching across the underscore
boundary. ``_check_origin_main_reachable``/``_sha_on_origin_main``
survive as private aliases (see just above ``_CHECK_SHIPPED_HELP``) purely because
``_stamp_node_shipped_sha`` and its test (``test_node_shipped_sha_stamp.py``) patch those
exact underscore paths; ``_resolve_ref`` had no such caller and was renamed outright.
Spec backlink: state/handoffs/2026-07-25_000823_shipped-state-verifier.md

Batch classification (2026-07-29): ``classify_shas_on_origin_main`` (private alias
``_classify_shas_on_origin_main``) replaces the per-SHA ``sha_on_origin_main`` spawn loop
inside ``_stamp_shipped_sha`` / ``_stamp_node_shipped_sha`` / ``_stamp_closure_reachability``
with two spawns total per stamp call, regardless of distinct-SHA count (measured 108 spawns
on a real corpus before this change).

CLI migration (2026-08-07): ``main()`` (the ``check-shipped-on-main.sh`` CLI port) now resolves
every argv ref first, then classifies every resolved sha in ONE ``classify_shas_on_origin_main``
call — the same many-commits-against-ONE-ref batchable shape, not a records-loop hot path but
the same tri-state contract. ``sha_on_origin_main`` itself is UNCHANGED and remains a public
helper (other in-repo callers may still classify a single sha), but ``main()`` no longer calls
it. Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a § C32.

Timeout kill (2026-08-21, hitlist § G5): ``fetch_origin_main`` is DELETED, not retuned —
a ``git fetch`` on an artifact-render path is the wrong mechanism at any bound, and its
120 was the only honestly-sized number in the group, which is precisely why it did not
belong here (`artifact.emit` max 56,646 ms, 33/72 non-ok). Every remaining git read in
this module derives its bound from ``git.run``'s single local-plumbing budget instead of
carrying one of the 60/120 house numbers; the two ``cat-file --batch-check`` sites feed
stdin, so they spawn directly but take the same constant by import.
Spec backlink: docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md § G5
Decision backlink: docs/decisions/DR-349-one-budget-governs-every-constructed-op.md
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from coordinator_core._settings_home import machine_local_dir, normalize_native_path
from coordinator_core.git.run import (
    LOCAL_PLUMBING_BUDGET_SECS,
    git_ok,
    git_out,
    run_git,
)
from coordinator_core.ops.emit.context import EmitContext


# competitor_summaries / intelligence_signals (cockpit-contract v2.16.0 market-intel widen):
# EMPTY-BY-DESIGN, PERMANENTLY. Claude-klabauter is NOT the data path for example-market-data-repo — that
# data routes example-market-data-repo -> cockpit's `ingestEmission` plane DIRECTLY, never through
# artifact.emit. Do NOT add a section `collect()`, do NOT read any market-intel source, do NOT
# register these two arrays in _PLACEMENT. Wiring them present-but-empty (skeleton keys below
# + malformed_records buckets in _MALFORMED_KEYS) is the confirmed TERMINAL state, not a stub
# awaiting a future porter.
#
# Authority: seam-confirm consult actioned by claude-central-em 2026-07-14
# (cross-repo/inbox/2026-07-14-claude-klabauter-em-cockpit-v2160-source-seam.md, ## EM Response).
#
# malformed_records key order (bash:2907-2923). Every key present, empty by default.
_MALFORMED_KEYS = (
    "handoffs", "backlogs", "review_trail", "coordinator_roots", "plans", "lessons",
    "cross_repo_memos", "roadmaps", "trackers", "health", "decision_guides",
    "session_hierarchies", "initiatives", "exec_summaries",
    "roadmap_dag_nodes", "roadmap_dag_edges", "competitor_summaries", "intelligence_signals",
    "commit_closures",
)

# Canonical output filename (bash:79). central_state_root / this = the default OUT_FILE.
DEFAULT_OUTPUT_NAME = "cockpit-emission.json"


# ---------------------------------------------------------------------------
# Section registry
# ---------------------------------------------------------------------------
SectionCollect = Callable[[EmitContext], "tuple[list, list]"]
# A place fn mutates the envelope in place, distributing (records, malformed).
SectionPlace = Callable[[dict, list, list], None]




























# ---------------------------------------------------------------------------
# Post-collect enrichment helpers
# ---------------------------------------------------------------------------



def check_origin_main_reachable(repo_root: Path) -> bool:
    """Return True when ``origin/main`` is locally reachable.

    The whole of this module's ancestry story rests on what the LOCAL repo already
    knows. When this returns False every caller below degrades — ``shipped_sha`` to
    null, ``reachable_on_default_branch`` to null — and that degradation is the
    contract, not a fallback to be papered over.

    Negative-spec:
      - Does NOT fetch, and no caller may fetch on its behalf. ``fetch_origin_main``
        (a ``git fetch origin main`` under a 120s bound) used to sit behind every
        False here, putting a network leg on the render path of an artifact: the
        observed failure mode was a two-minute stall inside a close ceremony, and
        `artifact.emit` measured a 56,646 ms maximum with 33 of 72 invocations
        non-ok. Emit renders what the repo knows; making the repo know more is a
        different op's job, run off the interactive path. DR-349 grants network legs
        no standing carve-out, and this one bought nothing an operator's own fetch
        does not already buy. Ledger precedent: K-002.
        Spec backlink: docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md § G5
      - Does NOT raise. A missing git, a timeout, or a repo with no ``origin``
        remote configured are all "not reachable", the same answer offline gives.
    """
    return git_ok(["-C", str(repo_root), "rev-parse", "origin/main"])


def sha_on_origin_main(repo_root: Path, sha: str) -> Optional[bool]:
    """Check if ``sha`` is an ancestor of ``origin/main`` via ``git merge-base --is-ancestor``.

    Returns True (on main), False (not on main), or None (sha unreachable / exit 2 equivalent).
    Parity: bash §1.5 ``check-shipped-on-main.sh <sha>`` exit 0/1/2 semantics.
    """
    result = run_git(["-C", str(repo_root), "merge-base", "--is-ancestor", sha, "origin/main"])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    # Any other exit code (128 = bad object / unreachable), a timeout (-1), or a
    # failed spawn (127) → treat as exit 2 (degrade all).
    return None


def classify_shas_on_origin_main(repo_root: Path, shas: list[str]) -> dict[str, Optional[bool]]:
    """Batch tri-state classification of ``shas`` against ``origin/main``, in O(1) git spawns.

    Per-SHA equivalent of calling ``sha_on_origin_main`` once per entry, but the whole set is
    resolved with exactly two subprocess spawns regardless of ``len(shas)`` — the 108-spawn
    hot spot this function replaces (one ``git merge-base --is-ancestor`` per record). Preserves
    ``sha_on_origin_main``'s tri-state contract element-for-element:

      - True  — sha is an ancestor of origin/main
      - False — sha is a valid, resolvable commit, but NOT an ancestor
      - None  — sha is unreachable / not a valid object (degrade-all signal to the caller)

    Mechanism: one ``git rev-list origin/main`` builds the ancestor set (anything printed there
    is, by definition, both a valid commit AND an ancestor); one ``git cat-file --batch-check``
    fed every REMAINING candidate sha on stdin classifies valid-commit-not-ancestor vs.
    not-a-valid-object — the distinction a naive ``rev-list`` set-membership test would collapse
    (silently reading "unreachable/bad object" as "definitely not shipped", a fail-open
    regression in a shipped-state signal). ``--batch-check`` emits exactly one output line per
    input line, in input order, even for objects that don't resolve — so line-for-line zip
    against the (deduplicated, ancestor-filtered) input list recovers per-sha classification
    without depending on ``%(objectname)`` matching an abbreviated or non-canonical input string.

    Any subprocess failure (git absent, timeout, OSError, or an output line count mismatch)
    degrades every entry to None, matching ``sha_on_origin_main``'s own except-clause policy.

    Both spawns are bounded by ``git.run``'s single local-plumbing budget rather than by
    a per-site literal. The pair carried ``timeout=120`` each until 2026-08-21: that number
    was sized for the 108-spawn per-record fan-out this function REPLACED, and survived the
    collapse to two spawns because nobody revisited it — the general case § G5 of the
    timeout hitlist names (the numbers outlive the defects that produced them). The
    ``cat-file`` leg feeds stdin, which ``run_git`` deliberately wires to DEVNULL, so it
    spawns directly and takes the same constant by import rather than by copy.
    """
    if not shas:
        return {}
    unique_shas = list(dict.fromkeys(shas))

    rev_list = run_git(["-C", str(repo_root), "rev-list", "origin/main"])
    if not rev_list.ok:
        return {sha: None for sha in unique_shas}
    ancestor_set = set(rev_list.stdout.split())

    # Only the SHAs NOT already proven ancestors need the validity check.
    candidates = [sha for sha in unique_shas if sha not in ancestor_set]
    valid_not_ancestor: set[str] = set()
    if candidates:
        stdin_payload = "\n".join(candidates) + "\n"
        try:
            batch_check = subprocess.run(
                ["git", "-C", str(repo_root), "cat-file", "--batch-check=%(objecttype)"],
                input=stdin_payload,
                capture_output=True,
                text=True,
                check=False,
                timeout=LOCAL_PLUMBING_BUDGET_SECS,
                **no_console_creationflags(),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {sha: None for sha in unique_shas}
        lines = batch_check.stdout.splitlines()
        if len(lines) != len(candidates):
            # Malformed/unexpected batch-check output shape — degrade everything rather
            # than risk misaligning a line to the wrong sha.
            return {sha: None for sha in unique_shas}
        for sha, line in zip(candidates, lines):
            if line.strip() == "commit":
                valid_not_ancestor.add(sha)

    result: dict[str, Optional[bool]] = {}
    for sha in unique_shas:
        if sha in ancestor_set:
            result[sha] = True
        elif sha in valid_not_ancestor:
            result[sha] = False
        else:
            result[sha] = None
    return result


# Back-compat private aliases — coordinator_core/ops/introspect/verify_shipped.py is a second,
# cross-module consumer of the four names above (promoted to public 2026-07-25, spec backlink
# state/handoffs/2026-07-25_000823_shipped-state-verifier.md). _stamp_node_shipped_sha below
# keeps calling these underscore-prefixed aliases rather than the public names: its own test
# (test_node_shipped_sha_stamp.py) patches these exact private paths via `unittest.mock.patch`,
# which rebinds only the attribute it names — a caller that referenced the public name instead
# would silently bypass the mock and spawn real git subprocesses. Do not rename this call site
# without also updating that test's patch targets.
_check_origin_main_reachable = check_origin_main_reachable
_sha_on_origin_main = sha_on_origin_main
_classify_shas_on_origin_main = classify_shas_on_origin_main


_CHECK_SHIPPED_HELP = """\
Usage: check-shipped-on-main.sh [--verbose] <commit> [<commit>...]

Checks whether all given commits are ancestors of origin/main.

Arguments:
  commit       A commit SHA, branch tip, or symbolic ref (e.g. HEAD).
               Accepts one or more.

Options:
  --verbose    Print one line per commit: "{sha}: ON_MAIN" or "{sha}: NOT_ON_MAIN ({age})"
  --help       Show this help

Exit codes:
  0  All commits are on origin/main.
  1  At least one commit is NOT on origin/main.
  2  Not inside a git repository, or origin/main is unreachable.
"""


def resolve_ref(repo_root: Path, ref: str) -> Optional[str]:
    """``git rev-parse <ref>``, returning the resolved SHA or None if unresolvable."""
    return git_out(["-C", str(repo_root), "rev-parse", ref]) or None


def _resolve_refs_batch(repo_root: Path, refs: list[str]) -> dict[str, Optional[str]]:
    """Resolve many refs to SHAs in ONE ``git cat-file --batch-check`` call.

    Replaces a per-ref ``resolve_ref`` (per-item ``git rev-parse``) loop in ``main``'s
    check-shipped-on-main path. ``--batch-check`` reads one object identifier per stdin
    line and emits exactly one output line per input, in order, using ``"<input> missing"``
    for anything it cannot resolve — a 1:1 positional correspondence that survives partial
    failure, unlike batching plain ``git rev-parse`` (whose stdout drops the failed args
    entirely, breaking any index-based zip back to the inputs). This is the "git cat-file
    --batch" batch form the plan's safe-primitive map names as known-correct; ``git
    rev-list``'s set-algebra batching trap does not apply here since each ref is resolved
    independently, not combined into a range.

    Bounded by ``git.run``'s local-plumbing budget, taken by import rather than by copy.
    This call feeds stdin, which ``run_git`` deliberately wires to DEVNULL, so it is one
    of the two sites in this module that still spawns directly.
    """
    if not refs:
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="\n".join(refs) + "\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=LOCAL_PLUMBING_BUDGET_SECS,
            **no_console_creationflags(),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {ref: None for ref in refs}

    lines = proc.stdout.split("\n")
    result: dict[str, Optional[str]] = {}
    for ref, line in zip(refs, lines):
        parts = line.split()
        if len(parts) >= 2 and parts[-1] != "missing" and len(parts[0]) == 40:
            result[ref] = parts[0]
        else:
            result.setdefault(ref, None)
    for ref in refs:
        result.setdefault(ref, None)
    return result


def _commit_age_labels_batch(repo_root: Path, shas: list[str]) -> dict[str, str]:
    """Batch form of ``_commit_age_label`` — ONE ``git show`` call for N shas.

    ``git show`` walks each positional argument as an independent object (unlike ``git
    log``'s revision-range set algebra), so batching commit lookups here is safe: each
    output block corresponds to its input sha, in the order given. Falls back to the
    per-sha "0s ago" quirk (age 0, faithfully reproducing ``_commit_age_label``'s own
    documented parity behaviour) for any sha the batch call could not resolve.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    if not shas:
        return {}
    by_sha: dict[str, int] = {}
    proc = run_git(
        ["-C", str(repo_root), "show", "--no-patch", "--format=%H%x09%ct", *shas]
    )
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            try:
                by_sha[parts[0]] = int(parts[1])
            except ValueError:
                continue

    result: dict[str, str] = {}
    for sha in shas:
        commit_ts = by_sha.get(sha, now)
        age_secs = now - commit_ts
        if age_secs < 3600:
            result[sha] = f"{age_secs}s ago"
        elif age_secs < 86400:
            result[sha] = f"{age_secs // 3600}h ago"
        else:
            result[sha] = f"{age_secs // 86400}d ago"
    return result




def main(argv: list[str]) -> int:
    """CLI entry — port of ``check-shipped-on-main.sh`` (git merge-base ancestor gate).

    Port of: check-shipped-on-main.sh (DoE b5a4192c, 2026-07-20).
    Reuses ``check_origin_main_reachable`` / ``classify_shas_on_origin_main``, the same
    ancestor-check helpers ``_stamp_shipped_sha`` exercises for envelope assembly.

    Negative-spec: read-only. Never modifies the repo, and — since 2026-08-21 — never
    contacts one either. An unreachable ``origin/main`` now exits 2 on the FIRST probe
    instead of attempting a fetch and re-probing; the exit code, its stderr line, and
    the tri-state per-commit contract are all unchanged, so a caller sees the same
    answer it saw whenever the fetch failed or was unnecessary. See
    ``check_origin_main_reachable``'s negative spec for why the fetch is gone.
    """
    verbose = False
    commits: list[str] = []
    for arg in argv:
        if arg in ("--verbose", "-v"):
            verbose = True
        elif arg in ("--help", "-h"):
            print(_CHECK_SHIPPED_HELP, end="")
            return 0
        elif arg.startswith("-"):
            print(f"Unknown option: {arg}", file=sys.stderr)
            return 1
        else:
            commits.append(arg)

    repo_root = Path.cwd()

    if not git_ok(["-C", str(repo_root), "rev-parse", "--is-inside-work-tree"]):
        print("check-shipped-on-main: not inside a git repository", file=sys.stderr)
        return 2

    if not check_origin_main_reachable(repo_root):
        print("check-shipped-on-main: origin/main is not reachable", file=sys.stderr)
        return 2

    if not commits:
        print(
            "check-shipped-on-main: no commits specified. Pass at least one SHA, branch, or HEAD.",
            file=sys.stderr,
        )
        return 1

    # Resolve every ref first (one `git rev-parse` per ref, unchanged — resolution of
    # branch names / symbolic refs / HEAD~N is out of this migration's scope), then
    # classify every resolved sha in ONE batched call rather than one
    # `sha_on_origin_main` spawn per ref. Many-commits-against-ONE-ref is the shape
    # `classify_shas_on_origin_main` already batches (its own docstring: two spawns
    # total regardless of set size) — distinct from the many-independent-RANGES shape,
    # which never batches (rev-list set-algebra collapse).
    resolved: dict[str, Optional[str]] = _resolve_refs_batch(repo_root, commits)
    shas_to_classify = [sha for sha in resolved.values() if sha is not None]
    classified = (
        classify_shas_on_origin_main(repo_root, shas_to_classify) if shas_to_classify else {}
    )

    any_not_on_main = 0
    age_labels: dict[str, str] = {}
    if verbose:
        not_on_main_shas = [
            resolved[ref]
            for ref in commits
            if resolved[ref] is not None and not classified.get(resolved[ref])
        ]
        age_labels = _commit_age_labels_batch(repo_root, not_on_main_shas)
    for ref in commits:
        sha = resolved[ref]
        if sha is None:
            print(f"check-shipped-on-main: cannot resolve '{ref}' — skipping", file=sys.stderr)
            any_not_on_main = 1
            continue

        short = sha[:8]
        # Explicit reconciliation (§ Anti-scope 25): a sha absent from the returned
        # classified map is NEVER read as a resolved classification — it degrades to
        # the same None (unreachable/indeterminate) branch classify_shas_on_origin_main
        # itself uses. classify_shas_on_origin_main's own contract returns an entry for
        # every requested sha, so this ``in`` check is a belt-and-suspenders guard
        # against a future contract change, not a case exercised by that function today.
        result = classified[sha] if sha in classified else None
        if result:
            if verbose:
                print(f"{short}: ON_MAIN")
        else:
            any_not_on_main = 1
            if verbose:
                age = age_labels.get(sha, "0s ago")
                print(f"{short}: NOT_ON_MAIN (committed {age})")

    return any_not_on_main


        # else shipped_sha stays null (collect() stub)












# ---------------------------------------------------------------------------
# Run-context resolution (engine wiring seam)
# ---------------------------------------------------------------------------
def _resolve_central_state_root(coordinator_root: Path, cwd: Path) -> Path:
    """Resolve ``coordinator_state_root --central`` natively (no bash spawn).

    Oracle: ``lib/coordinator-state-root.sh`` (DoE 6fb5fb37, 2026-07-22)'s
    ``coordinator_state_root --central`` with
    no ``--subject``/``--artifact`` — Rule 4 (backward-compat default): resolves to
    ``$(_csr_claude_klabauter_root)/state``. The bash lib's own ``_csr_claude_klabauter_root`` is itself
    documented as a native bridge onto ``coordinator_core.engine_root.coordinator_engine_root``
    (see that lib's "Native bridge" comment) — this calls the SAME native resolver directly,
    in-process, retiring the ``bash -c "source ... && coordinator_state_root --central"``
    spawn entirely. Falls back to the pre-migration computed path (~/.claude/state) only
    when the resolver fails, exactly as the previous bridge's except-clause did.

    ``coordinator_root`` and ``cwd`` are accepted for signature/call-site compatibility
    (the oracle's seam took both) but are not consulted: Rule 4's resolution depends only
    on the claude-klabauter-live-root resolver, never on the coordinator/cwd it's invoked from.

    Review: code-reviewer (F2/F3) — the except-clause is ``Exception`` (not the narrower
    ``(RuntimeError, ImportError)``) so it actually matches this docstring's "falls back
    ... only when the resolver fails" claim (``coordinator_engine_root()``'s pointer-file
    read only catches ``OSError``, so a corrupt/non-UTF-8 pointer file raises
    ``UnicodeDecodeError`` — a ``ValueError`` subclass the narrower clause missed and would
    have let crash ``emit()`` instead of degrading). The fallback also now ``warnings.warn``s
    — this project has already had one break-class incident from a *silent* legacy-path
    fallback on a durable-settings-home-only machine (unset ``repos.claude_klabauter``); a
    resolver failure here must be observable, not swallowed, matching the sibling pattern in
    ``resolve_coordinator_root``'s own env-override fallback warning below.
    """
    try:
        from coordinator_core.engine_root import coordinator_engine_root

        claude_klabauter_root = coordinator_engine_root()
        if claude_klabauter_root:
            return Path(claude_klabauter_root) / "state"
    except Exception as exc:  # noqa: BLE001 — any resolver failure degrades, per docstring
        import warnings

        warnings.warn(
            f"coordinator_engine_root() failed ({type(exc).__name__}: {exc}); falling back "
            "to the pre-migration CLAUDE_HOME/.claude/state path. This usually means "
            "repos.claude_klabauter is unset in the machine-local registry, or a "
            "partially-installed/durable-settings-home-only machine — verify "
            "`machine-local get repos.claude_klabauter` resolves before trusting emitted state.",
            stacklevel=2,
        )
    # pre-W4.2 assumption: coordinator_root was always 4 levels below ~/.claude; post-W4.2
    # it is the DoE clone at an arbitrary depth so .parent.parent.parent lands in the wrong
    # place. Derive from CLAUDE_HOME directly, same as resolve_context() does.
    return Path(os.environ.get("CLAUDE_HOME", str(Path.home()))) / ".claude" / "state"


def _registry_coordinator_root() -> Optional[Path]:
    """Registry rungs 3-4 of ``resolve_coordinator_root``, extracted for direct
    testability. Per-key precedence over the two machine-local registry files
    (``registry.local.toml`` wins, tracked ``registry.toml`` fills gaps —
    matches ``machine_resolver.registry_get``; the ``.local``-only read
    predated that pattern), key-major rung order preserved (``live_path``
    before ``repos.doe_claude``). Direct file reads only — bootstrap-safety
    invariant, no CLI/subprocess (see ``resolve_coordinator_root``'s
    docstring)."""
    reg_dir = machine_local_dir()
    # (parsed-dict-or-None, raw-text) per existing registry file, precedence-ordered.
    files: list = []
    for fname in ("registry.local.toml", "registry.toml"):
        path = reg_dir / fname
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        data = None
        try:
            try:
                import tomllib as _toml_mod  # stdlib 3.11+
            except ImportError:
                import tomli as _toml_mod  # type: ignore[no-redef]  # third-party fallback
            data = _toml_mod.loads(text)
        except Exception:
            import warnings
            warnings.warn(
                f"resolve_coordinator_root: tomllib/tomli unavailable or parse error on {fname}; "
                "falling back to quoted-key regex (may miss nested-table TOML forms). "
                "Install tomli or upgrade to Python 3.11+.",
                stacklevel=2,
            )
        files.append((data, text))

    for key_path, suffix in (
        (["plugin", "mirrors", "coordinator-claude", "live_path"], ""),
        (["repos", "doe_claude"], "/coordinator"),
    ):
        flat_key = ".".join(key_path)
        for data, text in files:
            if data is None:
                # Parse-failed file: quoted-key regex fallback on the raw text.
                ek = re.escape(flat_key)
                m = re.search(rf'"{ek}"\s*=\s*["\']([^"\']+)["\']', text)
                if m:
                    val = str(normalize_native_path(m.group(1).strip())) + suffix
                    candidate = Path(val).expanduser()
                    if (candidate / "bin" / "query-records.py").exists():
                        return candidate
                continue
            try:
                node: object = data
                for k in key_path:
                    node = node[k]  # type: ignore[index]
                candidate = Path(str(normalize_native_path(node)) + suffix).expanduser()
                if (candidate / "bin" / "query-records.py").exists():
                    return candidate
            except (KeyError, TypeError):
                # Nested-table form absent for this key_path -- fall through to
                # the flat quoted-compound-key form tried below.
                pass
            # Flat quoted-compound-key form: the machine-local registry CLI writes
            # "plugin.mirrors.coordinator-claude.live_path" = '...' as a single dotted
            # top-level key (tomllib parses it as-is, not as a nested structure).
            flat_val = data.get(flat_key)
            if flat_val is not None:
                try:
                    candidate = Path(str(normalize_native_path(flat_val)) + suffix).expanduser()
                    if (candidate / "bin" / "query-records.py").exists():
                        return candidate
                except (TypeError, ValueError):
                    # Flat-key value could not be normalized into a path -- try the
                    # next registry file / key_path candidate.
                    pass
    return None


def resolve_coordinator_root() -> Path:
    """Resolve the live post-W4.2-cutover coordinator script/lib root.

    Purpose: locate the coordinator clone whose ``bin/query-records.py`` exists so section
    porters that read from it find the real records reader.  The W4.2 cutover relocated the
    coordinator SOURCE out of ``~/.claude/plugins/coordinator-claude/coordinator/`` into the
    DoE clone at ``<doe-root>/coordinator``; the legacy plugin dir is now stale/empty.  The
    2026-07-22 de-node cutover then retired ``bin/query-records.js`` fleet-wide (claude-klabauter's own
    production dependency on it dropped to zero -- see
    ``cross-repo/archive/2026-07-22-claude-klabauter-em-query-records-positive-clearance-de-node-cutover-landed.md``
    -- and DoE-claude/coordinator/bin now carries neither the ``.js`` oracle nor a ``.py``
    port), so ``bin/query-records.py`` (claude-klabauter's own native, de-node-durable reader) is now
    the sentinel, not the deleted ``.js`` file.  Section porters that read from
    ``ctx.coordinator_root / "bin" / "query-records.py"`` would silently fall back to ``[]``
    (hollow emission) without this resolver.  Mirrors the bash oracle
    (``emit-cockpit-snapshot.sh``) which derives ``COORDINATOR_ROOT`` from its own
    script-location for the same reason.

    Resolution order (rungs 1-4 are direct file reads only — never CLI, per Design pin 4
    doctrine; rung 5 is the sole CLI-shellout exception, see below):
      1. ENV ``COORDINATOR_ROOT`` — set in CI or by the coordinator plugin loader.
      2. Co-located ``<claude-klabauter-repo-root>/coordinator`` (2026-07-22 executable-surface
         migration, commits b644d5a9/8a28a6ca): the coordinator bin/lib/scripts tree now
         lives INSIDE this repo, not the DoE-claude clone — DoE-claude/coordinator/bin is
         empty post-migration (PM ruling: coordinator scripts must not execute out of
         ``~/.claude``). Checked first because it is now the canonical, common case and
         needs no registry/subprocess round-trip. Mirrors ``data_root.py``'s
         ``_colocated_root()`` (same repo-root-relative ``/ "coordinator"`` landing spot).
      3. Machine-local registry key ``plugin.mirrors.coordinator-claude.live_path``
         (pre-migration W4.2 cutover path; kept for machines/fixtures whose registry
         still points at a valid DoE mirror). Per-key file precedence:
         ``registry.local.toml`` wins, tracked ``registry.toml`` fills gaps
         (``machine_resolver.registry_get`` semantics; see ``_registry_coordinator_root``).
      4. Machine-local registry key ``repos.doe_claude`` + ``/coordinator`` suffix
         (same two-file per-key precedence).
      5. ``resolve-coordinator-clone --for-content`` subprocess (survivor CLI at
         ``~/.claude/bin/resolve-coordinator-clone``) — replaces the pre-repoint legacy
         plugin dir read, which was stale/empty post-W4.2 cutover.

    Each candidate is validated by ``(candidate / "bin" / "query-records.py").exists()``.
    Raises ``RuntimeError`` with a diagnostic when no candidate passes.

    Shared production helper — imported by the parity test suite (``test_emit_parity.py``)
    instead of maintaining a duplicate copy.

    Bootstrap-safety invariant (load-bearing, AC2): the machine-local registry read (rungs 3-4
    above) is a deliberate direct-TOML file read via ``_settings_home.machine_local_dir()``,
    NEVER the ``machine-local`` CLI. This resolver runs on the coordinator-root bootstrap path,
    before PATH/CLI availability is guaranteed — shelling out to the CLI here would break
    bootstrap on a fresh checkout. See ``docs/plans/2026-07-11-coordinator-core-home-claude-read-repoint.md``
    § "bootstrap-safety invariant" for the doctrine reconciliation against the general
    registry-key-read CLI-shellout preference (``machine-local-registry.md:184``), which does
    NOT apply to this bootstrap-path subset.

    Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C2
    Spec backlink (settings-home repoint): pln-repoint-coordinator-core-claud-56d805 § C2
    """
    env_val = os.environ.get("COORDINATOR_ROOT", "").strip()
    if env_val:
        p = Path(env_val).expanduser()
        if (p / "bin" / "query-records.py").exists():
            return p
        # Review: code-reviewer (F3) — warn when the explicit COORDINATOR_ROOT override fails
        # validation; silent drop-through masks typos and stale/partial checkouts.
        import warnings
        warnings.warn(
            f"COORDINATOR_ROOT is set to {str(p)!r} but "
            f"{str(p / 'bin' / 'query-records.py')!r} is absent; "
            "the explicit override was ignored. Update COORDINATOR_ROOT to the coordinator "
            "plugin root or add a machine-local registry entry pointing at the DoE clone.",
            stacklevel=2,
        )

    # Rung 2 (2026-07-22 executable-surface migration) — see docstring rung 2 above.
    # coordinator_core/ops/emit/resolvers.py -> parent.parent.parent.parent == this
    # repo's root, then "/coordinator" to land on the co-located bin/lib/scripts tree.
    _colocated_root = Path(__file__).resolve().parent.parent.parent.parent / "coordinator"
    if (_colocated_root / "bin" / "query-records.py").exists():
        return _colocated_root

    # Direct TOML read (no CLI) — see bootstrap-safety invariant in the docstring above.
    registry_candidate = _registry_coordinator_root()
    if registry_candidate is not None:
        return registry_candidate

    # Legacy fallback: resolve the coordinator content root via the survivor CLI
    # (~/.claude/bin/resolve-coordinator-clone --for-content) rather than reading the
    # stale ~/.claude/plugins/coordinator-claude/coordinator plugin dir directly, which
    # is empty post-W4.2 cutover. Spec backlink:
    # docs/plans/2026-07-11-coordinator-core-home-claude-read-repoint.md § C4 (site 2).
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [str(Path.home() / ".claude" / "bin" / "resolve-coordinator-clone"), "--for-content"],
            capture_output=True,
            text=True,
            timeout=15,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        # Review: code-reviewer (Findings 2, 3) — added timeout=15 to match the
        # sibling C4 sites (liveness.py, and the OLD wsc_commit.py, retired
        # 2026-07-29 kill-list op removal) and resolve the absolute
        # survivor path instead of a bare PATH lookup for consistency.
        proc = None
    if proc is not None and proc.returncode == 0:
        legacy = Path(proc.stdout.strip())
        # Review: code-reviewer (F1) — validate query-records.py on the resolved path too;
        # returning on directory-existence alone re-introduces the hollow-emission bug if
        # the resolved clone is stale/partial.
        if (legacy / "bin" / "query-records.py").exists():
            return legacy
        raise RuntimeError(
            f"resolve-coordinator-clone --for-content returned {legacy} but "
            f"query-records.py not found at {legacy / 'bin' / 'query-records.py'}; "
            "this is a stale/partial checkout. Add a machine-local registry entry "
            "pointing at the DoE clone, or set COORDINATOR_ROOT to the coordinator plugin root."
        )
    raise RuntimeError(
        "coordinator root not found: checked COORDINATOR_ROOT env, the co-located "
        "<claude-klabauter-repo-root>/coordinator tree, machine-local registry, and "
        "resolve-coordinator-clone --for-content; "
        "set COORDINATOR_ROOT to the coordinator plugin root"
    )


def resolve_context(repo_root: Optional[Path] = None) -> EmitContext:
    """Build an EmitContext for the given repo root (or the meta-repo if none supplied).

    When ``repo_root`` is passed, the emission is rooted at that repo:
      - ``central_state_root`` is set to ``repo_root / "state"`` directly (no shell seam).
        # NOTE: holds a PER-REPO root post-2026-07-07 cutover, NOT central
      - Attribution (``repo_name``) is resolved from ``repo_root``'s own git remote.
    When ``repo_root`` is None (default), falls back to the pre-cutover behaviour:
    meta-repo (~/.claude) resolution via the shell seam.  This preserves backward
    compatibility for param-less callers (``artifact_emit.py:39``, ``goal_append.py:141``,
    ``recorder.py:173``) that are NOT wired until C4.

    ``coordinator_root`` = the LIVE post-W4.2-cutover coordinator script/lib clone,
    discovered via ``resolve_coordinator_root()`` regardless of which repo is emitting.

    Spec backlink: pln-per-repo-emission-cutover-un-h-03f05e § C2
    """
    coordinator_root = resolve_coordinator_root()
    if repo_root is not None:
        # Per-repo path: root state at repo_root/state directly, bypassing the --central seam.
        # NOTE: holds a PER-REPO root post-2026-07-07 cutover, NOT central
        central_state_root = repo_root / "state"
    else:
        # Legacy meta-repo path (pre-C4 callers; stays backward-compatible).
        claude_home = Path(os.environ.get("CLAUDE_HOME", str(Path.home())))
        repo_root = claude_home / ".claude"
        central_state_root = _resolve_central_state_root(coordinator_root, repo_root)
    return EmitContext.resolve(
        repo_root=repo_root,
        coordinator_root=coordinator_root,
        central_state_root=central_state_root,
    )


# ---------------------------------------------------------------------------
# C3 Section wiring — auto-wire all 21 section porters at import time.
# Lazy imports (inside the function body) avoid circular-import at the module
# level; the section modules import only from context.py, not from resolvers.py.
# Split sections (backlogs, rollups) supply explicit place fns. All others use
# the default single-target placer derived from SECMAP.
# ---------------------------------------------------------------------------



