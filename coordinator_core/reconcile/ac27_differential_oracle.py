"""
coordinator_core.reconcile.ac27_differential_oracle -- differential verdict oracle
comparing gate_eval's pre-widening and post-widening evaluators across the live+
archived handoff corpus of all five fleet repos (COMPUTE_ONLY, read-only).

Purpose: C4-C7 (docs/plans/2026-07-26-gate-resolution-widen-and-migrate.md, DoE-claude)
widened gate_eval.evaluate_gate's structured-path eligibility, chased `continued_into`
to a terminus, added an external-gate evidence class, and gave dangling `blocked_by`
refs a disposition. The expected first-pass flip count across the live corpus is
ZERO, which means no fixture-shaped acceptance criterion can catch a widening that
over-clears something it should not -- a fixture only ever catches a failure mode
someone already imagined. This module is the oracle for that gap (AC27/C12 GATE): it
loads gate_eval.py exactly as it existed immediately before C4 and immediately after
C7 landed, runs BOTH over the IDENTICAL live+archived data for every `awaiting_gate`
baton in DoE-claude, claude-klabauter, example-cockpit-repo, example-retrieval-repo, and
Example-market-data-repo, and reports every verdict delta for human attribution against
the four named widenings.

Two axes, kept legible as two axes (never collapsed into one delta list): the
EVALUATOR axis above (fixed corpus, two evaluator snapshots) catches a widening
that over-clears; the CORPUS axis (fixed evaluator, two corpus states -- a named
git ref versus the working tree) catches the complementary blind spot the
evaluator axis structurally cannot see -- a migration that rewrites `blocked_by`-
adjacent prose (e.g. `gate_dependency` -> `blocking_notes`) out from under an
UNCHANGED evaluator. Varying the evaluator against a fixed corpus can never
observe that; varying the corpus against a fixed evaluator is the only axis that
can. See `run_corpus_axis` / `run_both_axes`.

Negative-spec:
    - Time-travels `gate_eval.py`'s OWN source only, not its transitive imports.
      `_load_gate_eval_at` git-shows `gate_eval.py`'s text at each pinned sha and
      execs it into an isolated module namespace, but that module's `import`
      statements (e.g. `from coordinator_core.lifecycle_constants import
      HANDOFF_TERMINAL_DEPLOYMENT`) still resolve against the process's live
      `sys.modules` -- i.e. the CURRENT working-tree version of anything it
      imports, not the version that existed at PRE_C4_SHA/POST_C7_SHA. Verified
      empirically as of 2026-07-27 that `HANDOFF_TERMINAL_DEPLOYMENT` is
      byte-identical at PRE_C4_SHA and HEAD, so this has not produced a wrong
      delta to date -- but that is a property of this window's history, not
      something the evaluator axis itself enforces. `run()` now calls
      `_check_transitive_import_isolation` to detect (not prevent) a future
      divergence in `_GATE_EVAL_TRANSITIVE_IMPORT_PATHS` and surfaces it in the
      `transitive_import_isolation_warnings` key rather than silently resting
      on the same luck forever; that check does not cover the full transitive
      import graph, only the paths named in that tuple.
    - Never writes to any repo's state/handoffs/ or archive/ tree -- read-only over
      frontmatter dicts, and the two gate_eval snapshots are read via `git show
      <sha>:path` into an in-memory string, never checked out or written to disk.
      The corpus axis's older-ref handoffs are likewise read via `git show
      <ref>:<path>` strings -- never `git checkout`, so the working tree the live
      axis reads is never touched.
    - Never invokes handoff.reconcile_open, handoff.ship_and_archive, or
      handoff.transition gate-cascade-clear -- calls gate_eval.evaluate_gate()
      directly, bypassing every mutating caller and every op-level side effect
      (D1 conservation history, D2 dry_run policy resolution, etc).
    - Never resolves a repo root by a literal path -- always through
      coordinator_core.machine_resolver.registry_get("repos.<name>"), so this
      module carries no machine-specific path and needs no edit to run on a
      differently-laid-out machine.
    - Never assembles `gate_evidence` from cross-repo state itself -- mirrors
      production's own call shape (`witness_candidates=None`, no `gate_evidence`
      argument), since `handoff.reconcile_open` does not wire either today; a
      verdict delta attributable to a not-yet-wired capability would be a false
      positive against this oracle's own comparison basis.
    - The corpus axis never mutates its evaluator choice implicitly -- it is
      always an explicit pinned snapshot (a named sha, loaded the same way the
      evaluator axis loads one), never `coordinator_core.reconcile.gate_eval`
      imported live off HEAD. HEAD is a moving target other chunks in this same
      plan are actively editing; a diff against a live import would silently
      become a test of whatever landed most recently instead of a test of one
      fixed evaluator's behaviour across two corpus states.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_THIS_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_THIS_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_THIS_REPO_ROOT))

from coordinator_core.dag import _parse_frontmatter  # noqa: E402
from coordinator_core.machine_resolver import registry_get  # noqa: E402
from coordinator_core.ops.ceremony.git_native import (  # noqa: E402
    cat_file_batch,
    cat_file_batch_objects,
)
from coordinator_core.ops.handoff_reconcile import (  # noqa: E402
    _AWAITING_GATE_STATE,
    _collect_all_handoffs_for_gate_index,
    _collect_open_handoffs,
    _is_open,
)

#: (registry key, human label) for the five fleet repos this oracle spans. Order
#: is report-display order only -- not load-bearing.
REPO_KEYS: Tuple[Tuple[str, str], ...] = (
    ("repos.doe_claude", "DoE-claude"),
    ("repos.claude_klabauter", "claude-klabauter"),
    ("repos.example_cockpit_repo", "example-cockpit-repo"),
    ("repos.example_retrieval_repo", "example-retrieval-repo"),
    ("repos.example_market_data_repo", "example-market-data-repo"),
)

#: gate_eval.py at the commit immediately BEFORE C4 landed -- the parent of
#: 8c41e3ea ("C9+C4: lineage is not gating; reconcile the two evaluators and
#: widen once"). `evaluate_gate_triage` and `_classify_blocked_by` already exist
#: at this SHA (c23d9a91 landed them earlier); what does NOT yet exist is
#: `_is_structured_gate`, the C4 prose-dominance rule inside `evaluate_gate`, the
#: C5 `continued_into` chase, the C6 external-gate evidence class, and the C7
#: `resolved_without_baton` disposition read.
PRE_C4_SHA = "578c47d3"

#: gate_eval.py as of C5+C6+C7 landing (7e69ecc1, "chase continuations, wire
#: external-gate evidence, dispose danglers") -- the post-widening evaluator
#: this plan's C12 GATE / AC27 names explicitly. Commits after this one that
#: touch gate_eval.py (C19+C19c, da7e3fe4) are docstring/path-attachment fixes,
#: not verdict-logic changes -- this oracle deliberately pins to 7e69ecc1 rather
#: than HEAD because AC27's own text names "post-C7", not "current".
POST_C7_SHA = "7e69ecc1"

#: 2026-08-05 provenance note: DR-266 (docs/plans/2026-07-26-gate-resolution-widen-
#: and-migrate.md's follow-on, DoE-claude) adds a `contradiction` key to
#: `evaluate_gate`'s prose-dominance return dict when `staleness_evidence` is not
#: None, and re-routes `evaluate_gate_triage`'s review-due path -- both landing
#: strictly AFTER POST_C7_SHA. This oracle's evaluator axis loads `gate_eval.py`
#: ONLY at PRE_C4_SHA and POST_C7_SHA (never HEAD, per the module docstring's
#: negative-spec), so a `run()` pass over the current tree cannot see the
#: DR-266 delta at all -- zero flips here is NOT evidence DR-266 introduced no
#: verdict change; it is the oracle's pinned window ending before DR-266 begins.
#: Do not repin POST_C7_SHA (or add a third pinned sha) to chase this: whether
#: this window should widen again is a scoping decision for whoever next reasons
#: about AC27's coverage, not a side effect of landing DR-266 itself. Also note
#: this oracle's `run()`/`_evaluate` never calls `evaluate_gate_triage` at any
#: sha -- it differentials `evaluate_gate` against itself across the two pinned
#: snapshots, so the triage-side half of DR-266 is outside this oracle's
#: comparison shape entirely, independent of which shas are pinned.
#:
#: The evaluator the corpus axis pins for BOTH corpus states it compares. Reuses
#: the evaluator-axis's own post-widening snapshot rather than importing
#: `gate_eval` live -- see the module docstring's negative-spec on why HEAD is
#: unsafe to import here while sibling chunks are mid-edit on it.
CORPUS_AXIS_EVALUATOR_SHA = POST_C7_SHA

#: `git ls-tree` roots this oracle reads the handoff corpus from at an older ref
#: -- mirrors the three trees `_collect_open_handoffs` /
#: `_collect_all_handoffs_for_gate_index` walk live (state/handoffs/ plus both
#: archive roots), so the ref-based read sees the same corpus shape the live
#: read does, just at a different point in the tree's history.
_CORPUS_TREE_ROOTS: Tuple[str, ...] = ("state/handoffs", "archive/handoffs", "archive/completed")


def _load_gate_eval_at(claude_klabauter_root: Path, sha: str, module_name: str) -> types.ModuleType:
    """Load coordinator_core/reconcile/gate_eval.py as it existed at `sha` as an
    independent in-memory module -- never checked out, never written to disk.

    `module_name` must be unique per call: both snapshots (and the live module,
    if anything else in-process has already imported it) must coexist under
    distinct `sys.modules` keys, or the second `exec` would silently rebind the
    first snapshot's globals.
    """
    from coordinator_core.win_portability import no_console_creationflags

    source = subprocess.run(
        ["git", "-C", str(claude_klabauter_root), "show", f"{sha}:coordinator_core/reconcile/gate_eval.py"],
        check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    exec(compile(source, f"<{module_name}@{sha}>", "exec"), module.__dict__)
    return module


#: Review: coordinator:code-reviewer -- modules `gate_eval.py` imports beyond
#: stdlib/typing. Only `gate_eval.py`'s OWN source is time-travelled by
#: `_load_gate_eval_at` (see that function's docstring); anything it imports
#: resolves through the process's live `sys.modules`, i.e. the CURRENT
#: working-tree version, not the version pinned at PRE_C4_SHA/POST_C7_SHA.
#: This tuple is the cheap divergence guard's coverage list, not full
#: transitive-import isolation -- see `_check_transitive_import_isolation`.
_GATE_EVAL_TRANSITIVE_IMPORT_PATHS: Tuple[str, ...] = (
    "coordinator_core/lifecycle_constants.py",
)


def _check_transitive_import_isolation(
    claude_klabauter_root: Path, pre_sha: str, post_sha: str,
) -> List[str]:
    """Detect (never silently assume) whether a module `gate_eval.py` imports
    has changed shape between `pre_sha` and `post_sha`.

    `_load_gate_eval_at` only git-shows `gate_eval.py` itself at each pinned
    sha; its `import` statements still resolve against the live working tree,
    so BOTH pinned snapshots transparently share whatever the current
    checkout has for e.g. `lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT`.
    That is fine as long as the imported module is byte-identical across the
    window -- but nothing enforced that until this check. Returns a list of
    human-readable divergence warnings (empty when every covered path is
    byte-identical between the two shas); does NOT achieve full transitive
    isolation (this only covers `_GATE_EVAL_TRANSITIVE_IMPORT_PATHS`, not the
    full import graph) -- see that tuple's docstring.
    """
    warnings: List[str] = []
    # ONE `git cat-file --batch` feed for every (sha, path) pair this check
    # needs, via `cat_file_batch_objects` — the same promoted primitive
    # `_gated_batons_for_repo_at_ref` uses below — instead of one `git show`
    # spawn per path per sha. Each `<rev>:<path>` spec resolves independently
    # (no set-algebra hazard, per that helper's own docstring), so batching
    # here changes no comparison outcome: a missing/unreadable blob still
    # resolves to None on its own slot, exactly like `_git_show_blob`'s
    # returncode-!=-0 -> None contract.
    objects = [f"{pre_sha}:{p}" for p in _GATE_EVAL_TRANSITIVE_IMPORT_PATHS] + [
        f"{post_sha}:{p}" for p in _GATE_EVAL_TRANSITIVE_IMPORT_PATHS
    ]
    resolved = cat_file_batch_objects(claude_klabauter_root, objects)
    n = len(_GATE_EVAL_TRANSITIVE_IMPORT_PATHS)
    for i, rel_path in enumerate(_GATE_EVAL_TRANSITIVE_IMPORT_PATHS):
        pre_source = resolved.get(objects[i])
        post_source = resolved.get(objects[n + i])
        if pre_source != post_source:
            warnings.append(
                f"{rel_path} differs between {pre_sha} and {post_sha} -- "
                "gate_eval.py's import of this module is NOT time-travelled "
                "(only gate_eval.py's own source is), so both pinned "
                "snapshots silently shared whichever version is live in the "
                "working tree. Any delta attribution resting on this run "
                "should be re-checked by hand."
            )
    return warnings


def _resolve_repo_root(registry_key: str) -> Path:
    """Resolve `registry_key` (e.g. "repos.example_retrieval_repo") via the machine-local
    registry -- never a literal path. Raises RuntimeError (never silently skips
    a repo) when the key is unset or resolves to something that isn't a live
    directory, since a silently-dropped repo would understate this oracle's
    corpus coverage without saying so."""
    value = registry_get(registry_key)
    if not value:
        raise RuntimeError(f"machine-local registry has no entry for {registry_key!r}")
    root = Path(value)
    if not root.is_dir():
        raise RuntimeError(f"{registry_key} resolves to {root}, which is not a directory")
    return root


def _gated_batons_for_repo(
    repo_root: Path,
) -> "Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]":
    """Return (awaiting_gate_batons, all_live_and_archived, scan_errors) for
    `repo_root`, assembled with the SAME collectors and the SAME open-set /
    awaiting-gate predicate `handoff.reconcile_open` itself uses -- so this
    oracle sees precisely the data production would see, never a hand-rolled
    approximation of it that could itself introduce a spurious delta.
    """
    open_handoffs = _collect_open_handoffs(repo_root)
    all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(repo_root)
    awaiting = [
        h for h in open_handoffs
        if (h.get("deployment_state") or "").strip().lower() == _AWAITING_GATE_STATE
    ]
    return awaiting, all_handoffs, scan_errors


def _git_ls_corpus_files(repo_root: Path, ref: str) -> List[str]:
    """List every `.md` path under `_CORPUS_TREE_ROOTS` as it existed at `ref`,
    via `git ls-tree` (never a working-tree walk) -- so a corpus-axis read at an
    older ref sees exactly what was committed then, including files the working
    tree has since deleted/moved (the migration's "disappeared" side) and never
    a path the working tree has added since (the migration's "appeared" side).
    """
    from coordinator_core.win_portability import no_console_creationflags

    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", ref, "--", *_CORPUS_TREE_ROOTS],
        capture_output=True, text=True,
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.endswith(".md")]


def _git_show_blob(repo_root: Path, ref: str, rel_path: str) -> Optional[str]:
    """Read `rel_path` as it existed at `ref` via `git show <ref>:<path>` into an
    in-memory string -- never `git checkout`, so the working tree the live/new
    corpus read relies on is never disturbed by reading the old corpus state."""
    from coordinator_core.win_portability import no_console_creationflags

    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{rel_path}"],
        capture_output=True, text=True,
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _gated_batons_for_repo_at_ref(
    repo_root: Path, ref: str,
) -> "Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]":
    """Corpus-axis counterpart to `_gated_batons_for_repo`: same
    (awaiting_gate_batons, all_live_and_archived, scan_errors) shape, but the
    corpus is read from `ref` via git rather than from the working tree.
    Reuses `_parse_frontmatter` (the same frontmatter parser `_read_meta` calls
    on file bytes) and `_is_open` (the same open-set predicate
    `_collect_open_handoffs` applies) so a ref-based read classifies a baton
    identically to how a working-tree read at that same content would.

    Reads every blob via ONE `coordinator_core.ops.ceremony.git_native
    .cat_file_batch` feed instead of one `git show` spawn per file (the N+1
    site this function used to be). That helper was authored locally here
    (C5) and promoted to the shared git-wrapper module (C36) once a second
    call site needed the same primitive -- see its docstring there.

    `scan_errors` is always `[]` here -- an unreadable blob at a pinned ref is a
    hard failure surfaced by raising, not a soft scan gap; unlike a live
    filesystem walk, there is no partial/racing state a historical git read
    can land in.
    """
    rel_paths = _git_ls_corpus_files(repo_root, ref)
    contents = cat_file_batch(repo_root, ref, rel_paths)
    all_handoffs: List[Dict[str, Any]] = []
    for rel_path in rel_paths:
        content = contents.get(rel_path)
        if content is None:
            raise RuntimeError(
                f"corpus axis: git cat-file --batch could not resolve "
                f"{ref}:{rel_path} in {repo_root} -- "
                "ls-tree listed this path but the batch feed could not read it"
            )
        meta = _parse_frontmatter(content)
        if not meta:
            continue
        meta = dict(meta)
        meta["_path"] = str(repo_root / rel_path)
        meta.setdefault("id", meta.get("id") or Path(rel_path).stem)
        all_handoffs.append(meta)
    awaiting = [
        h for h in all_handoffs
        if _is_open(h) and (h.get("deployment_state") or "").strip().lower() == _AWAITING_GATE_STATE
    ]
    return awaiting, all_handoffs, []


def _baton_key(handoff: Dict[str, Any]) -> str:
    """Correlation key for matching one baton across two corpus states.
    `id` is the durable identity `blocked_by`/gate resolution itself keys on
    (see `_collect_all_handoffs_for_gate_index`'s own docstring on durable-id
    surviving an archive move) -- the SAME key an archive move must not change,
    which is exactly the property this axis needs to tell "moved" apart from
    "appeared" or "disappeared"."""
    return str(handoff.get("id") or handoff.get("_path") or "")


def _evaluate(
    module: types.ModuleType,
    handoff: Dict[str, Any],
    all_handoffs: List[Dict[str, Any]],
    scan_errors: List[str],
) -> Dict[str, Any]:
    """Invoke `module.evaluate_gate` with the exact argument shape
    `handoff.reconcile_open` uses in production (see that op's own call site) --
    `witness_candidates=None`, `scan_incomplete` derived from `scan_errors`, no
    `gate_evidence` (production does not wire it yet either)."""
    return module.evaluate_gate(
        handoff, all_handoffs, witness_candidates=None,
        scan_incomplete=len(scan_errors) > 0, scan_errors=scan_errors,
    )


def run() -> Dict[str, Any]:
    """Run the full five-repo differential pass and return a JSON-serializable
    result dict: per-repo baton counts + deltas, and a flat top-level delta
    list for the acceptance check (every entry here must be attributed to a
    named C4-C7 widening by the caller; an entry that cannot be is the
    over-clearing signal this oracle exists to surface)."""
    pre = _load_gate_eval_at(_THIS_REPO_ROOT, PRE_C4_SHA, "_ac27_gate_eval_pre_c4")
    post = _load_gate_eval_at(_THIS_REPO_ROOT, POST_C7_SHA, "_ac27_gate_eval_post_c7")
    transitive_import_isolation_warnings = _check_transitive_import_isolation(
        _THIS_REPO_ROOT, PRE_C4_SHA, POST_C7_SHA,
    )

    per_repo: Dict[str, Any] = {}
    deltas: List[Dict[str, Any]] = []
    total_batons = 0

    for registry_key, label in REPO_KEYS:
        root = _resolve_repo_root(registry_key)
        awaiting, all_handoffs, scan_errors = _gated_batons_for_repo(root)
        total_batons += len(awaiting)
        repo_deltas: List[Dict[str, Any]] = []
        for handoff in awaiting:
            pre_v = _evaluate(pre, handoff, all_handoffs, scan_errors)
            post_v = _evaluate(post, handoff, all_handoffs, scan_errors)
            if pre_v.get("verdict") != post_v.get("verdict"):
                entry = {
                    "repo": label,
                    "handoff_id": handoff.get("id"),
                    "path": handoff.get("_path"),
                    "pre_verdict": pre_v.get("verdict"),
                    "post_verdict": post_v.get("verdict"),
                    "pre_evidence": pre_v.get("evidence"),
                    "post_evidence": post_v.get("evidence"),
                }
                repo_deltas.append(entry)
                deltas.append(entry)
        per_repo[label] = {
            "root": str(root),
            "awaiting_gate_count": len(awaiting),
            "scan_incomplete": len(scan_errors) > 0,
            "scan_errors": scan_errors,
            "delta_count": len(repo_deltas),
            "deltas": repo_deltas,
        }

    return {
        "pre_sha": PRE_C4_SHA,
        "post_sha": POST_C7_SHA,
        "total_awaiting_gate_batons": total_batons,
        "total_deltas": len(deltas),
        "per_repo": per_repo,
        "deltas": deltas,
        "transitive_import_isolation_warnings": transitive_import_isolation_warnings,
    }


def run_corpus_axis(old_corpus_ref: str = "HEAD") -> Dict[str, Any]:
    """Run the complementary axis: ONE pinned evaluator (`CORPUS_AXIS_EVALUATOR_SHA`)
    over TWO corpus states -- the repo at `old_corpus_ref` and the live working
    tree -- across all five repos. Returns a result dict shaped like `run()`'s,
    but the two axes are never merged into one delta list (see module
    docstring): a caller wanting a single "did anything change" answer must
    look at BOTH `run()["deltas"]` and this function's `["deltas"]`, not assume
    either alone is complete.

    `appeared` (present at the live corpus, absent at `old_corpus_ref`) and
    `disappeared` (the reverse -- e.g. shipped-and-archived-out-of-scope, or a
    path rename `git ls-tree` at the old ref cannot follow) are reported
    SEPARATELY from `deltas`: neither is a verdict delta, because there is no
    verdict pair to compare where a baton was not awaiting_gate at both ends.
    Collapsing either bucket into `deltas` would misreport "this baton did not
    exist yet" as "this baton's verdict moved," which is a distinct and
    incorrect claim.
    """
    evaluator = _load_gate_eval_at(
        _THIS_REPO_ROOT, CORPUS_AXIS_EVALUATOR_SHA, "_ac27_gate_eval_corpus_axis_fixed",
    )

    per_repo: Dict[str, Any] = {}
    deltas: List[Dict[str, Any]] = []
    appeared: List[Dict[str, Any]] = []
    disappeared: List[Dict[str, Any]] = []
    total_batons = 0

    for registry_key, label in REPO_KEYS:
        root = _resolve_repo_root(registry_key)
        old_awaiting, old_all, _old_scan_errors = _gated_batons_for_repo_at_ref(root, old_corpus_ref)
        new_awaiting, new_all, new_scan_errors = _gated_batons_for_repo(root)
        total_batons += len(new_awaiting)

        old_by_key = {_baton_key(h): h for h in old_awaiting}
        new_by_key = {_baton_key(h): h for h in new_awaiting}

        repo_deltas: List[Dict[str, Any]] = []
        repo_appeared: List[Dict[str, Any]] = []
        repo_disappeared: List[Dict[str, Any]] = []

        for key, new_handoff in new_by_key.items():
            old_handoff = old_by_key.get(key)
            if old_handoff is None:
                repo_appeared.append({"repo": label, "handoff_id": key, "path": new_handoff.get("_path")})
                continue
            old_v = _evaluate(evaluator, old_handoff, old_all, [])
            new_v = _evaluate(evaluator, new_handoff, new_all, new_scan_errors)
            if old_v.get("verdict") != new_v.get("verdict"):
                entry = {
                    "repo": label,
                    "handoff_id": key,
                    "path": new_handoff.get("_path"),
                    "old_corpus_verdict": old_v.get("verdict"),
                    "new_corpus_verdict": new_v.get("verdict"),
                    "old_corpus_evidence": old_v.get("evidence"),
                    "new_corpus_evidence": new_v.get("evidence"),
                }
                repo_deltas.append(entry)
                deltas.append(entry)

        for key, old_handoff in old_by_key.items():
            if key not in new_by_key:
                repo_disappeared.append({"repo": label, "handoff_id": key, "path": old_handoff.get("_path")})

        appeared.extend(repo_appeared)
        disappeared.extend(repo_disappeared)
        per_repo[label] = {
            "root": str(root),
            "old_corpus_awaiting_gate_count": len(old_awaiting),
            "new_corpus_awaiting_gate_count": len(new_awaiting),
            "delta_count": len(repo_deltas),
            "deltas": repo_deltas,
            "appeared": repo_appeared,
            "disappeared": repo_disappeared,
        }

    return {
        "fixed_evaluator_sha": CORPUS_AXIS_EVALUATOR_SHA,
        "old_corpus_ref": old_corpus_ref,
        "new_corpus_state": "working_tree",
        "total_new_corpus_awaiting_gate_batons": total_batons,
        "total_deltas": len(deltas),
        "total_appeared": len(appeared),
        "total_disappeared": len(disappeared),
        "per_repo": per_repo,
        "deltas": deltas,
        "appeared": appeared,
        "disappeared": disappeared,
    }


def run_both_axes(old_corpus_ref: str = "HEAD") -> Dict[str, Any]:
    """Run the evaluator axis (`run`) and the corpus axis (`run_corpus_axis`) and
    return them under distinct top-level keys -- deliberately not flattened
    into one shared `deltas` list, so a reader can always tell which axis
    produced a given entry (see module docstring)."""
    return {
        "evaluator_axis": run(),
        "corpus_axis": run_corpus_axis(old_corpus_ref),
    }


def main(argv: List[str]) -> int:
    """CLI entrypoint: `python3 -m coordinator_core.reconcile.ac27_differential_oracle`.
    Prints the full result as JSON to stdout; exit code is always 0 -- this is a
    reporting instrument, not a gate that fails the process (the human/EM
    reading its `deltas` list is the actual gate, per the C12 GATE text)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--old-corpus-ref", default="HEAD",
        help="git ref the corpus axis reads as the OLDER corpus state (default: HEAD, "
             "i.e. compare the last commit's corpus against the working tree -- the "
             "shape an in-flight migration takes before it is committed).",
    )
    args = parser.parse_args(argv)
    result = run_both_axes(args.old_corpus_ref)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
