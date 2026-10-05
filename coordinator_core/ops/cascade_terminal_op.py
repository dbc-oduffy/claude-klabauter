"""
coordinator_core.ops.cascade_terminal_op — JSON-RPC "deliverable.cascade_terminal" (v2).

Purpose: advance every live handoff and sizing joined to one `deliverable_id` when its
source reaches a terminal state, and land every advanced path in ONE commit. Process
time under the 200ms bar with 0 spawns: the dispatch-path costs the library
(`ops/deliverable_cascade.py`) carries are struck, not shaved.

Params: `deliverable_id`, `source_kind` ("plan" | "handoff"), `source_path` (all required);
`ship_sha` (7-40 hex, optional) — the commit that landed the source's terminal transition.
There is no kind-selection param: both kinds always run, handoff then sizing.

Reply: `{"exit_code", "deliverable_id", "by_kind": {"handoff": {...}, "sizing": {...}},
"commit_sha", "commit_error"?, "error"?}`; each `by_kind` value carries `candidates_matched`,
`advanced`, `refused`, `already_advanced`, `scan_incomplete`, `unreadable`. `exit_code` is 0 iff
any kind advanced.

Kept from the library: DR-263 legs a-d (`_predicate_refusal`, composed), validate-before-write,
`locked_rmw`, the sizing write (`_advance_one_sizing`).

Struck on the dispatch path (verdict: docs/research/spike-verdicts/2026-10-03-cascade-terminal-v2.md):
the scope-derived `shipped_in` fallback and `resolve_source_ship_sha` (each spawns git; a
handoff without `ship_sha` is refused by name), the fixpoint re-scan, one commit per kind,
AC6g baton-row depth, the full-corpus YAML parse, and the per-candidate thread hop.

Negative-spec:
  - Does NOT edit or re-decorate `ops/deliverable_cascade.py`; the library stays for
    `plan_status_transition._run_cascade`.
  - Does NOT spawn a subprocess; `commit_paths` is spawn-free on the common case.
  - Does NOT widen leg (d) or exempt any spinoff.
  - Does NOT flip a handoff without `ship_sha`; there is no guessed evidence.
"""

from __future__ import annotations

import os
import re
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from coordinator_core.session import record_homes
from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import format_validation_errors
from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.index_write import IndexStaleAfterCommit, IndexWriteError
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.deliverable_cascade import (
    _ALREADY_ADVANCED_MARKER,
    _HANDOFF_KIND,
    _SIZING_KIND,
    _plan_fk_matches,
    _advance_one_sizing,
    _compose_cascade_commit_message,
    _iso_now,
    _predicate_refusal,
    _read_sizing_meta,
    _validate_fm,
)
from coordinator_core.ops.fleet._common import main_worktree_root

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

_NO_SHIP_SHA_REASON = (
    "no ship evidence: 'ship_sha' was not supplied — the commit that landed the source's "
    "terminal transition is required to flip a handoff; refusing, not flipped"
)


def _read_corpus_texts(base_dir: Path, suffix: str) -> tuple[Dict[str, str], bool]:
    """Raw text of every `*suffix` file under `base_dir`, keyed by abspath, plus an
    incomplete flag (directory missing or unenumerable)."""
    texts: Dict[str, str] = {}
    if not base_dir.is_dir():
        return texts, True
    try:
        entries = sorted(os.scandir(base_dir), key=lambda e: e.name)
    except OSError:
        return texts, True
    for entry in entries:
        if not entry.name.endswith(suffix) or not entry.is_file():
            continue
        try:
            with open(entry.path, "r", encoding="utf-8", errors="replace") as fh:
                texts[os.path.abspath(entry.path)] = fh.read()
        except OSError:
            continue
    return texts, False


def _collect_handoffs(
    texts: Dict[str, str], deliverable_id: str
) -> List[dict]:
    """Live handoffs whose own `deliverable_id` exact-matches. A substring test on the raw
    text gates the YAML parse, so only matches are parsed."""
    matches: List[dict] = []
    for path_str, text in texts.items():
        if deliverable_id not in text:
            continue
        fm = _read_meta(path_str)
        if not fm:
            continue
        did = fm.get("deliverable_id")
        if not isinstance(did, str) or did.strip() != deliverable_id:
            continue
        if fm.get(_HANDOFF_KIND.lifecycle_field) in _HANDOFF_KIND.terminal_values:
            continue
        matches.append({"path": Path(path_str), "fm": fm})
    return matches


def _collect_sizings(
    texts: Dict[str, str], deliverable_id: str, plan_fk: str = ""
) -> tuple[List[dict], List[dict]]:
    """Live sizings whose `deliverable_id` exact-matches or, for a plan source, whose
    `plan` field names `plan_fk` (a sizing routed before its plan minted a deliverable
    id carries `deliverable_id: null`), plus the matches that failed to parse (AC6a:
    surfaced as unreadable, never a silent zero)."""
    matches: List[dict] = []
    unreadable: List[dict] = []
    for path_str, text in texts.items():
        if deliverable_id not in text and not (plan_fk and plan_fk in text):
            continue
        try:
            fm = _read_sizing_meta(path_str)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            unreadable.append({"path": path_str, "reason": str(exc)})
            continue
        did = fm.get("deliverable_id")
        did_match = isinstance(did, str) and did.strip() == deliverable_id
        if not did_match and not (plan_fk and _plan_fk_matches(fm.get("plan"), plan_fk)):
            continue
        if fm.get(_SIZING_KIND.lifecycle_field) in _SIZING_KIND.terminal_values:
            continue
        matches.append({"path": Path(path_str), "fm": fm})
    return matches, unreadable


def _leg_b_metas(
    candidate_path: Path, candidate_fm: dict, texts: Dict[str, str]
) -> Dict[str, dict]:
    """Corpus metas for leg (b) with real frontmatter only where a referencer is possible.

    An edge to the candidate names its filename stem or its `handoff_id`, so a handoff
    whose raw text carries neither cannot reference it; it gets an edgeless `{}` stub the
    index builder accepts without re-reading the file. Pinned against the unprefiltered
    `has_live_children_from_metas` by test_cascade_terminal_op.py.
    """
    needles = [candidate_path.stem]
    hid = candidate_fm.get("handoff_id")
    if isinstance(hid, str) and hid.strip():
        needles.append(hid.strip())
    candidate_abs = os.path.abspath(str(candidate_path))
    metas: Dict[str, dict] = {}
    for path_str, text in texts.items():
        if path_str == candidate_abs:
            metas[path_str] = candidate_fm
        elif any(n in text for n in needles):
            metas[path_str] = _read_meta(path_str)
        else:
            metas[path_str] = {}
    return metas


def _flip_handoff(
    candidate_path: Path,
    deliverable_id: str,
    advanced_at: str,
    ship_sha: str,
    common_dir: Path,
) -> tuple[bool, Optional[str]]:
    """Flip one handoff to shipped in a single locked write: `shipped_in`,
    `shipped_in_kind`, `deployment_state`, `pickup_ready`, `advanced_by`, `advanced_at`,
    validated once. Returns (advanced, refusal_reason); never raises."""
    state = {"applied": False}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"advance: no parseable YAML frontmatter for {candidate_path}")
        fm = split.fm_text
        if read_fm_field_unquoted(fm, "deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT:
            return old_text

        ship_anchor = "claimed_at" if read_fm_field(fm, "claimed_at") is not None else "consumed_at"
        for key, value, anchor, numeric in (
            ("shipped_in", ship_sha, ship_anchor, True),
            ("shipped_in_kind", "ship-commit", "shipped_in", False),
            ("deployment_state", "shipped", "status", False),
            ("pickup_ready", "false", "deployment_state", False),
            ("advanced_by", deliverable_id, "deployment_state", False),
            ("advanced_at", advanced_at, "advanced_by", False),
        ):
            if read_fm_field(fm, key) is not None:
                fm = replace_fm_field(fm, key, value, numeric_quoting=numeric)
            else:
                fm = insert_fm_field(fm, key, value, after_key=anchor, numeric_quoting=numeric)

        errors = _validate_fm(fm)
        if errors:
            raise MutateAbort(
                f"advance: post-mutation schema validation failed: {format_validation_errors(errors)}"
            )
        state["applied"] = True
        return rebuild(split, fm)

    try:
        locked_rmw(candidate_path, mutate, repo_root=common_dir)
    except FileNotFoundError:
        return False, f"advance: handoff not found: {candidate_path}"
    except LockTimeout as exc:
        return False, f"advance: timed out waiting for file lock on {candidate_path}: {exc}"
    except MutateAbort as exc:
        return False, (exc.args[0] if exc.args else "advance: mutation aborted")
    if state["applied"]:
        return True, None
    return False, _ALREADY_ADVANCED_MARKER


def _kind_result(candidates: int, scan_incomplete: bool, unreadable: List[dict]) -> dict:
    return {
        "candidates_matched": candidates,
        "advanced": [],
        "refused": [],
        "already_advanced": [],
        "scan_incomplete": scan_incomplete,
        "unreadable": unreadable,
    }


def _rel(path: Path, worktree: Path) -> str:
    try:
        return path.resolve().relative_to(worktree.resolve()).as_posix()
    except ValueError:
        return str(path)


def _commit(
    paths: List[str], worktree: Path, deliverable_id: str
) -> tuple[Optional[str], Optional[str]]:
    """One `commit_paths` call over exactly `paths`. Returns (commit_sha, commit_error)."""
    message = apply_missing_trailers(
        _compose_cascade_commit_message(deliverable_id, paths), worktree, paths
    )
    try:
        outcome = commit_paths(
            worktree,
            paths,
            message,
            prefer_deliberate_stage=True,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree),
        )
    except IndexStaleAfterCommit as exc:
        return exc.outcome.sha, None
    except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
        return None, f"deliverable.cascade_terminal: commit failed: {exc}"
    return outcome.sha, None


def _require_params(params: dict) -> tuple[str, str, str, str]:
    deliverable_id = str(params.get("deliverable_id") or "").strip()
    source_kind = str(params.get("source_kind") or "").strip()
    source_path = str(params.get("source_path") or "").strip()
    ship_sha = str(params.get("ship_sha") or "").strip()
    if not deliverable_id:
        raise ValueError("deliverable.cascade_terminal: 'deliverable_id' is required")
    if source_kind not in ("plan", "handoff"):
        raise ValueError(
            f"deliverable.cascade_terminal: 'source_kind' must be 'plan' or 'handoff', got {source_kind!r}"
        )
    if not source_path:
        raise ValueError("deliverable.cascade_terminal: 'source_path' is required")
    if ship_sha and not _SHA_RE.match(ship_sha):
        raise ValueError(
            f"deliverable.cascade_terminal: 'ship_sha' must be 7-40 hex characters, got {ship_sha!r}"
        )
    return deliverable_id, source_kind, source_path, ship_sha.lower()


@register_op("deliverable.cascade_terminal")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """deliverable.cascade_terminal — advance the handoffs and sizings joined to one deliverable.

    `repo_root` is the git COMMON dir (the directory ending in `.git`), never the worktree
    root. Raises `ValueError` on a missing or malformed required param, or no `repo_root`.
    A candidate that clears no predicate leg or has no ship evidence is named in `refused`,
    never flipped. See the module docstring for the reply shape.
    """
    deliverable_id, source_kind, source_path, ship_sha = _require_params(params)
    if repo_root is None:
        raise ValueError("deliverable.cascade_terminal: repo_root is required (no founding root available)")
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)
    advanced_at = _iso_now()

    handoff_texts, handoff_incomplete = _read_corpus_texts(Path(record_homes.home_dir(str(worktree), "handoffs")), ".md")
    sizing_texts, sizing_incomplete = _read_corpus_texts(Path(record_homes.home_dir(str(worktree), "sizings")), ".yaml")

    handoffs = _collect_handoffs(handoff_texts, deliverable_id)
    if source_kind == "handoff":
        source = Path(source_path)
        absolute = source if source.is_absolute() else worktree / source
        resolved_source = contained_path(absolute, [Path(record_homes.home_dir(str(worktree), "handoffs"))])
        if resolved_source is not None:
            handoffs = [c for c in handoffs if c["path"].resolve() != resolved_source]
    plan_fk = ""
    plan_fk_unresolved = False
    if source_kind == "plan":
        source = Path(source_path)
        absolute = source if source.is_absolute() else worktree / source
        try:
            plan_fk = absolute.resolve().relative_to(worktree.resolve()).as_posix()
        except (OSError, ValueError):
            plan_fk_unresolved = True

    sizings, sizing_unreadable = _collect_sizings(sizing_texts, deliverable_id, plan_fk)

    handoff_result = _kind_result(
        len(handoffs), handoff_incomplete, []
    )
    sizing_result = _kind_result(
        len(sizings), sizing_incomplete or bool(sizing_unreadable), sizing_unreadable
    )
    mutated: List[str] = []

    for candidate in handoffs:
        path: Path = candidate["path"]
        entry_path = str(path)
        refusal = await _predicate_refusal(
            path,
            candidate["fm"],
            common_dir,
            kind=_HANDOFF_KIND,
            corpus_metas=_leg_b_metas(path, candidate["fm"], handoff_texts),
        )
        if refusal is None and not ship_sha:
            refusal = _NO_SHIP_SHA_REASON
        if refusal is not None:
            handoff_result["refused"].append({"path": entry_path, "reason": refusal})
            continue
        did_advance, write_refusal = _flip_handoff(
            path, deliverable_id, advanced_at, ship_sha[:8], common_dir
        )
        _record(handoff_result, mutated, worktree, path, did_advance, write_refusal,
                f"advanced (deployment_state: shipped, advanced_by: {deliverable_id})")

    for candidate in sizings:
        path = candidate["path"]
        refusal = await _predicate_refusal(
            path, candidate["fm"], common_dir, kind=_SIZING_KIND
        )
        if refusal is not None:
            sizing_result["refused"].append({"path": str(path), "reason": refusal})
            continue
        did_advance, write_refusal = _advance_one_sizing(path, plan_fk, common_dir)
        _record(sizing_result, mutated, worktree, path, did_advance, write_refusal,
                f"advanced (status: shipped{', plan: ' + plan_fk if plan_fk else ''})")
        if did_advance and plan_fk_unresolved:
            sizing_result["advanced"][-1]["plan_fk_unresolved"] = source_path

    commit_sha: Optional[str] = None
    commit_error: Optional[str] = None
    if mutated:
        commit_sha, commit_error = _commit(mutated, worktree, deliverable_id)

    any_advanced = bool(handoff_result["advanced"] or sizing_result["advanced"])
    result: dict = {
        "exit_code": 0 if any_advanced else 1,
        "deliverable_id": deliverable_id,
        "by_kind": {"handoff": handoff_result, "sizing": sizing_result},
        "commit_sha": commit_sha,
    }
    if commit_error:
        result["commit_error"] = commit_error
    if not any_advanced:
        matched = len(handoffs) + len(sizings)
        result["error"] = (
            f"deliverable.cascade_terminal: no live handoff or sizing carries "
            f"deliverable_id={deliverable_id!r} — nothing to advance"
            if not matched
            else f"deliverable.cascade_terminal: {matched} candidate(s) matched "
            f"deliverable_id={deliverable_id!r} but none advanced — see 'refused'"
        )
    return result


def _record(
    kind_result: dict,
    mutated: List[str],
    worktree: Path,
    path: Path,
    did_advance: bool,
    write_refusal: Optional[str],
    message: str,
) -> None:
    entry_path = str(path)
    if did_advance:
        kind_result["advanced"].append({"path": entry_path, "message": message})
        mutated.append(_rel(path, worktree))
    elif write_refusal == _ALREADY_ADVANCED_MARKER:
        kind_result["already_advanced"].append({"path": entry_path, "reason": write_refusal})
    else:
        kind_result["refused"].append({"path": entry_path, "reason": write_refusal or "not advanced"})
