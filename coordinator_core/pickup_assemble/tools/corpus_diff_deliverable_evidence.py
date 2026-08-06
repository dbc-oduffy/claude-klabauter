"""
coordinator_core.pickup_assemble.tools.corpus_diff_deliverable_evidence — a
before/after corpus-diff harness for `compute_deliverable_evidence`, built to
prove byte-identical output across the `state/handoffs/` corpus ahead of a
speed rewrite of that function (or its `compute_handoff_preflight` caller).

Scaffolding only: this module never edits `pickup_assemble` behavior. It
drives the real `compute_handoff_preflight` caller path (not a hand-rolled
scope_paths/since_date derivation) so a capture exercises exactly what
`brief()` exercises for a live handoff pickup.

A `capture` run pins `HEAD` to a single sha resolved once at the start, so a
peer commit landing mid-walk on a concurrently-edited branch cannot skew
later artifacts against a different tree than earlier ones. The pinned sha
is recorded in the output; `compare` refuses (loudly) to diff two captures
pinned to different shas, since that comparison reflects capture-time drift,
not a behaviour change.

The revision pin does NOT protect the discovered `state/handoffs/` FILE SET
— that's the live working tree, walked fresh by each `capture` invocation.
Two `capture` runs are two separate process invocations, potentially
minutes apart during dev iteration; a handoff added/archived on disk
between them (a concurrent commit, an operator's own archive-sweep) is
corpus drift, not a `compute_deliverable_evidence` behaviour change. Each
capture snapshots its discovered file list into the output, and `compare`
flags any difference between the two lists as its own loud, explicitly
labeled "FILE-SET DRIFT" category, distinct from the field-level diffs
below — never silently folded in as an ordinary "present only in
before/after" line.

Usage:
    python3 -m coordinator_core.pickup_assemble.tools.corpus_diff_deliverable_evidence capture <out.json> [--revision <rev>]
    python3 -m coordinator_core.pickup_assemble.tools.corpus_diff_deliverable_evidence compare <before.json> <after.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import coordinator_core.pickup_assemble as _pickup_assemble
from coordinator_core.pickup_assemble import (
    _ArtifactElisionInconclusive,
    _ArtifactUnreadable,
    compute_handoff_preflight,
    resolve_artifact,
    resolve_repo_root,
)

#: Cap on how many mismatching artifacts `compare` prints in full before
#: collapsing the remainder into a single "N more" line — keeps a
#: whole-corpus regression readable instead of scrolling the terminal.
_MAX_REPORTED_MISMATCHES = 25

#: Top-level output keys — the pinned revision the capture ran against, the
#: discovered handoff file list (snapshotted so `compare` can flag file-set
#: drift explicitly, see module docstring), and the per-handoff evidence
#: map. Kept as siblings (not merged) so `compare` can check the pin and the
#: file set before touching a single artifact's field-level diff.
_PINNED_REVISION_KEY = "pinned_revision"
_DISCOVERED_KEY = "discovered_relpaths"
_ARTIFACTS_KEY = "artifacts"


def _resolve_pin(root: Path, revision: str) -> str:
    """Resolves `revision` (`"HEAD"` by default) to a concrete sha exactly
    once per capture run, via the same `_resolve_revision` machinery
    `pickup_assemble` itself uses. Raises `RuntimeError` on an unresolvable
    repo or revision — `capture` has nothing useful to pin without this."""
    discovered = _pickup_assemble._discover_git_dirs(root)
    if discovered is None:
        raise RuntimeError(f"corpus-diff: could not discover git dirs under {root}")
    _, dirs = discovered
    sha = _pickup_assemble._resolve_revision(dirs, revision)
    if sha is None:
        raise RuntimeError(f"corpus-diff: could not resolve revision {revision!r}")
    return sha


@contextmanager
def _pin_head_for_capture(pinned_sha: str) -> Iterator[None]:
    """Monkeypatches `pickup_assemble._resolve_revision` so every `"HEAD"`
    lookup made while capturing resolves to `pinned_sha` for the whole run,
    instead of re-resolving the branch tip per artifact — without this, a
    peer's commit landing mid-capture on a concurrently-edited branch makes
    later artifacts evaluate against a different tree than earlier ones,
    and the resulting "diff" is capture-skew, not a behaviour change.
    Restores the original resolver in `finally` so the process is left
    clean; acceptable only because this is a test/verification harness, not
    a runtime path."""
    original = _pickup_assemble._resolve_revision

    def _pinned_resolve_revision(dirs: Any, value: str) -> str | None:
        if value == "HEAD":
            return pinned_sha
        return original(dirs, value)

    _pickup_assemble._resolve_revision = _pinned_resolve_revision
    try:
        yield
    finally:
        _pickup_assemble._resolve_revision = original


def _discover_handoffs(root: Path) -> list[Path]:
    """Every `*.md` under `state/handoffs/` (recursive), sorted for a
    deterministic walk order regardless of filesystem enumeration order."""
    handoffs_dir = root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return []
    return sorted(handoffs_dir.rglob("*.md"), key=lambda p: p.as_posix())


#: Exception classes an unanticipated artifact shape can raise out of
#: `compute_handoff_preflight`'s frontmatter/body parsing and evidence
#: computation — malformed YAML shapes, missing dict keys, wrong field
#: types. Deliberately NOT a bare `except Exception`: this must stay
#: narrow enough that a genuine bug in the harness itself (a `NameError`
#: from a typo, say) still aborts loudly instead of being swallowed as a
#: per-artifact error.
_PREFLIGHT_EXCEPTIONS = (KeyError, ValueError, TypeError, AttributeError, OSError)


def _capture_one(root: Path, relpath: str) -> dict[str, Any]:
    """Runs the real `resolve_artifact` -> `compute_handoff_preflight` caller
    path for one handoff and returns its `deliverable_evidence`, mirroring
    what `brief()` computes for a live pickup rather than re-deriving
    scope_paths/since_date independently. A handoff whose frontmatter yields
    no scope carries an explicit empty list, not a dropped key — a silently
    skipped artifact would make the corpus diff lie. Resolution failures
    (unreadable/elision-inconclusive) and `compute_handoff_preflight`
    failures on one bad artifact are both captured as an explicit error
    marker rather than aborting the whole corpus walk and discarding every
    artifact already processed — `compare` surfaces a mismatched error
    marker exactly like any other field-level diff (see `_diff_one_handoff`),
    so an artifact that errors in one run and succeeds in the other is never
    silently treated as equal."""
    try:
        artifact = resolve_artifact(relpath, root)
    except (_ArtifactUnreadable, _ArtifactElisionInconclusive) as exc:
        return {"error": str(exc)}

    fm = artifact.get("frontmatter") or {}
    scope_entries = fm.get("scope", []) or []
    try:
        preflight = compute_handoff_preflight(root, artifact, fm, scope_entries)
    except _PREFLIGHT_EXCEPTIONS as exc:
        return {"error": f"compute_handoff_preflight raised {exc.__class__.__name__}: {exc}"}
    return {"deliverable_evidence": preflight["deliverable_evidence"]}


def _run_capture(out_path: Path, revision: str = "HEAD") -> int:
    """`capture` subcommand — writes a deterministic, sorted-key,
    absolute-path-free JSON document: the git revision this run pinned to
    (resolved once, up front) plus a mapping of every discovered handoff to
    its captured `compute_deliverable_evidence` output. `revision` defaults
    to whatever HEAD is right now; pass an explicit sha to pin a run to a
    caller-chosen tip (e.g. to force two runs onto the same known revision)."""
    root = resolve_repo_root()
    if root is None:
        print("corpus-diff: could not resolve a git worktree root", file=sys.stderr)
        return 2

    try:
        pinned_sha = _resolve_pin(root, revision)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    results: dict[str, Any] = {}
    discovered: list[str] = []
    with _pin_head_for_capture(pinned_sha):
        for handoff_path in _discover_handoffs(root):
            relpath = handoff_path.relative_to(root).as_posix()
            discovered.append(relpath)
            results[relpath] = _capture_one(root, relpath)

    output = {
        _PINNED_REVISION_KEY: pinned_sha,
        _DISCOVERED_KEY: discovered,
        _ARTIFACTS_KEY: results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        json.dump(output, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(
        f"corpus-diff: captured {len(results)} artifact(s) pinned to "
        f"{pinned_sha} -> {out_path}"
    )
    return 0


def _evidence_by_path(entry: dict[str, Any]) -> dict[str, Any]:
    """Indexes one handoff's `deliverable_evidence` list by scope `path` so
    `compare` can name which specific scope path changed, not just that the
    handoff's evidence list differs somewhere."""
    evidence = entry.get("deliverable_evidence")
    if not isinstance(evidence, list):
        return {}
    return {item.get("path"): item for item in evidence if isinstance(item, dict)}


def _diff_one_handoff(relpath: str, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Field-level diff for a single handoff — reports the specific scope
    path and field that changed, an artifact-resolution error appearing or
    disappearing, or a scope path present in only one capture."""
    lines: list[str] = []

    before_error = before.get("error")
    after_error = after.get("error")
    if before_error != after_error:
        lines.append(f"{relpath}: resolution error changed: {before_error!r} -> {after_error!r}")
        return lines

    before_by_path = _evidence_by_path(before)
    after_by_path = _evidence_by_path(after)
    all_paths = sorted(set(before_by_path) | set(after_by_path))

    for scope_path in all_paths:
        before_item = before_by_path.get(scope_path)
        after_item = after_by_path.get(scope_path)
        if before_item is None:
            lines.append(f"{relpath}: scope path {scope_path!r} present only in after")
            continue
        if after_item is None:
            lines.append(f"{relpath}: scope path {scope_path!r} present only in before")
            continue
        for field in sorted(set(before_item) | set(after_item)):
            if before_item.get(field) != after_item.get(field):
                lines.append(
                    f"{relpath}: scope path {scope_path!r} field {field!r} changed: "
                    f"{before_item.get(field)!r} -> {after_item.get(field)!r}"
                )

    return lines


def _file_set_drift_lines(before_doc: dict[str, Any], after_doc: dict[str, Any]) -> list[str]:
    """Compares the two captures' snapshotted `_discover_handoffs` file
    lists and reports any difference as its own explicitly labeled
    "FILE-SET DRIFT" category — distinct from `_diff_one_handoff`'s
    field-level diffs, since a handoff added/archived between two capture
    runs is corpus drift, not a `compute_deliverable_evidence` behaviour
    change (see module docstring). Falls back to the `artifacts` map's key
    set for captures written before `_DISCOVERED_KEY` existed, so an old
    capture file doesn't spuriously report every artifact as drifted."""
    before_files = set(before_doc.get(_DISCOVERED_KEY) or before_doc.get(_ARTIFACTS_KEY, {}))
    after_files = set(after_doc.get(_DISCOVERED_KEY) or after_doc.get(_ARTIFACTS_KEY, {}))

    lines: list[str] = []
    for relpath in sorted(after_files - before_files):
        lines.append(f"FILE-SET DRIFT: {relpath!r} discovered only in after (added between captures)")
    for relpath in sorted(before_files - after_files):
        lines.append(f"FILE-SET DRIFT: {relpath!r} discovered only in before (removed between captures)")
    return lines


def _run_compare(before_path: Path, after_path: Path) -> int:
    """`compare` subcommand — exits 0 with a one-line OK summary when the
    two captures are identical; on any difference, exits 1 and prints every
    file-set-drift line followed by every mismatching artifact's field-level
    diff, capped at `_MAX_REPORTED_MISMATCHES` with an explicit "N more"
    tail line.

    Fails loudly (exit 2, before touching a single artifact) when the two
    captures were pinned to different revisions — that comparison is
    meaningless noise, not a behaviour signal, and must never silently
    fall through to a normal diff or a false OK."""
    before_doc = json.loads(before_path.read_text(encoding="utf-8"))
    after_doc = json.loads(after_path.read_text(encoding="utf-8"))

    before_pin = before_doc.get(_PINNED_REVISION_KEY)
    after_pin = after_doc.get(_PINNED_REVISION_KEY)
    if not before_pin or not after_pin:
        print(
            "corpus-diff: capture is missing its pinned revision — re-capture "
            "both files with the current tool before comparing",
            file=sys.stderr,
        )
        return 2
    if before_pin != after_pin:
        print(
            f"corpus-diff: PINNED REVISION MISMATCH — {before_path} was captured "
            f"at {before_pin}, {after_path} at {after_pin}. Comparing captures "
            "taken at different tips is meaningless (a concurrent commit between "
            "the two runs, not a behaviour change); re-capture both against the "
            "same revision and compare again.",
            file=sys.stderr,
        )
        return 2

    drift_lines = _file_set_drift_lines(before_doc, after_doc)

    before = before_doc.get(_ARTIFACTS_KEY, {})
    after = after_doc.get(_ARTIFACTS_KEY, {})

    all_relpaths = sorted(set(before) & set(after))
    diff_lines: list[str] = []
    for relpath in all_relpaths:
        diff_lines.extend(_diff_one_handoff(relpath, before[relpath], after[relpath]))

    mismatches = drift_lines + diff_lines
    if not mismatches:
        print(f"corpus-diff: OK — {len(all_relpaths)} artifact(s) identical")
        return 0

    for line in mismatches[:_MAX_REPORTED_MISMATCHES]:
        print(line)
    remainder = len(mismatches) - _MAX_REPORTED_MISMATCHES
    if remainder > 0:
        print(f"... {remainder} more")
    print(
        f"corpus-diff: MISMATCH — {len(drift_lines)} file-set drift issue(s), "
        f"{len(diff_lines)} field-level diff line(s) across {len(all_relpaths)} "
        "artifact(s) compared"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint — dispatches `capture`/`compare` per the module
    docstring's usage contract."""
    parser = argparse.ArgumentParser(prog="corpus-diff-deliverable-evidence")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("out_path", type=Path)
    capture_parser.add_argument(
        "--revision",
        default="HEAD",
        help="git revision to pin the capture to, resolved once at the start "
        "of the run (default: HEAD)",
    )

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("before_path", type=Path)
    compare_parser.add_argument("after_path", type=Path)

    args = parser.parse_args(argv)

    if args.subcommand == "capture":
        return _run_capture(args.out_path, args.revision)
    return _run_compare(args.before_path, args.after_path)


if __name__ == "__main__":
    sys.exit(main())
