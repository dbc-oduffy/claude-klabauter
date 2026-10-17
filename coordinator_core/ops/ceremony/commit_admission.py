"""coordinator_core.ops.ceremony.commit_admission -- the doctrine-surface
admission check at the one point every engine commit passes through.

The admission predicate (`hooks.claude_md_ledger.admission_check_for_surface`)
governs growth of the `GOVERNED_AUTHORING_SURFACES` -- the always-on boot
payload. Its enforcement points are tool-call guards: PreToolUse on Write/Edit
and on Bash. An engine op that rewrites one of those files in-process makes no
tool call, and every engine commit lands through `_commit_via_head_spine` or
its plumbing ladder, neither of which runs a git hook. So an op-driven change
to the boot payload reached the tree with admission never consulted, and the
surfaces grew past their recorded watermarks by exactly that route.

`governed_surface_refusal` runs the same predicate against the exact blobs a
commit is about to land. `_commit_via_head_spine` calls it before any of its
own preconditions, so a refusal also stops the ladder: the ladder only runs
when the head spine returns `None`, and a refusal is a failing result, never
`None`.

SCOPE: a surface is enforced only where its ledger exists. A repo that has
never adopted a classification ledger for its `CLAUDE.md` has no admission
policy to enforce, and the predicate's no-ledger disposition (all growth
refused) would freeze that file on every commit. That disposition stays with
the Write/Edit guard, which is where it was authored.

FAILS CLOSED once a governed, ledgered path is touched: an unreadable blob, a
malformed ledger, or any exception evaluating it refuses the commit with the
cause named. Shrinking a surface is always admitted by the predicate itself,
and a deletion is a shrink.

`skill_weight_check` is the coordinator-content-repo skill-weight leg, run beside it.
It FAILS OPEN, unlike the leg above: DoE's guard is fail-open by contract, so
a plumbing failure (unreadable baseline, predicate import error) admits the
commit and yields a `check did not run` note that the commit reply carries as
a warning; only a measured violation refuses. It loads
the committing repo's own predicate and harness by file path, restores
`sys.path` and drops every `sys.modules` key the load added, and spawns nothing.

Two further entry points carry the same predicate to the other places the
boot payload changes without a tool call:
  - `governed_write_refusal` is for an op that rewrites a file in-process: it
    asks before writing, so an op never leaves refused text in the tree.
  - `uncommitted_surface_refusals` compares the working tree with HEAD. Text
    that is written but not yet committed is already in the next session's boot
    payload, and no commit has checked it. A SessionStart hook reports it.

Negative-spec:
    Does NOT spawn: HEAD blobs come from `head_blobs` (in-process tree walk)
    and new blobs from `read_object`.
    `governed_surface_refusal` does NOT read the working tree for the
    surface's content -- only the blobs being committed. The LEDGER is read from the working tree, as every other
    enforcement point reads it, so a commit that classifies a section and
    grows it can land together.
    Does NOT carry an override key; the remedy is the ledger (classify the
    section, or bump the watermark with a reason), exactly as on Write/Edit.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from functools import partial
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from coordinator_core.git.commit import checkin_sizes, hash_worktree_blobs_via_spawn
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import _parse_tree_entries, head_blobs, read_tree_spine
from coordinator_core.hooks.claude_md_ledger import (
    GOVERNED_AUTHORING_SURFACES,
    admission_check_for_surface,
    resolve_ledger_path,
)


def _blob_text(common_dir: Path, sha: str) -> Optional[str]:
    obj = read_object(common_dir, sha)
    if obj is None or obj[0] != "blob":
        return None
    return obj[1].decode("utf-8", errors="replace")


def governed_surface_refusal(root: Path, assembled: Mapping[str, object]) -> Optional[str]:
    root = Path(root)
    governed = [
        path
        for path in assembled
        if path in GOVERNED_AUTHORING_SURFACES and resolve_ledger_path(root, path).is_file()
    ]
    if not governed:
        return None

    common_dir = resolve_git_common_dir(root)
    old_blobs = head_blobs(root, governed)
    refusals = []
    for path in governed:
        entry = assembled[path]
        try:
            old_entry = old_blobs.get(path)
            old_text = "" if old_entry is None else _blob_text(common_dir, old_entry[1])
            new_text = (
                _blob_text(common_dir, entry[1])
                if isinstance(entry, tuple) and len(entry) == 2
                else ""
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed, see module docstring.
            refusals.append(_incomplete(path, exc))
            continue
        if old_text is None or new_text is None:
            refusals.append(
                f"{path}: a blob this commit would land could not be read, so its "
                "admission could not be checked -- refusing rather than landing an "
                "unchecked change to a governed doctrine surface."
            )
            continue
        refusal = governed_write_refusal(root, path, old_text, new_text)
        if refusal is not None:
            refusals.append(refusal)
    return "\n".join(refusals) if refusals else None


_SKILLS_PREFIX = "coordinator/skills/"
_SW_BASELINE = "coordinator/tests/baselines/skill-weight-budget.json"
_SW_ADMISSION = "coordinator/hooks/scripts/_skill_weight_admission.py"
_SW_HARNESS = "coordinator/tests/fixtures/skill-weight/skill_weight_harness.py"
_SW_TAG = "[skill-weight-budget-guard]"
_LOG = logging.getLogger(__name__)


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree_blob_sizes(common_dir: Path, tree_sha: str, prefix: str, out: Dict[str, int]) -> None:
    obj = read_object(common_dir, tree_sha)
    if obj is None or obj[0] != "tree":
        raise ValueError(f"tree {tree_sha} unreadable")
    entries = _parse_tree_entries(obj[1])
    if entries is None:
        raise ValueError(f"tree {tree_sha} malformed")
    for name, (mode, sha) in entries.items():
        if mode == 0o40000:
            _tree_blob_sizes(common_dir, sha, f"{prefix}{name}/", out)
        elif mode != 0o160000:
            blob = read_object(common_dir, sha)
            if blob is None or blob[0] != "blob":
                raise ValueError(f"blob {sha} unreadable")
            out[f"{prefix}{name}"] = len(blob[1])


def _head_skill_sizes(root: Path, common_dir: Path) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    spine = read_tree_spine(root, [_SKILLS_PREFIX + "x"])
    if spine is None:
        return sizes
    parent = spine.get("coordinator")
    skills = parent.get("skills") if parent else None
    if skills is not None and skills[0] == 0o40000:
        _tree_blob_sizes(common_dir, skills[1], _SKILLS_PREFIX, sizes)
    return sizes


def skill_weight_check(
    root: Path, assembled: Mapping[str, object]
) -> Tuple[Optional[str], Optional[str]]:
    """`(refusal, note)` for the assembled tree: refuse, admit `(None, None)`,
    or could-not-run `(None, note)`. A weight verdict never yields a note.

    Runs the committing repo's own `_skill_weight_admission` predicate and
    harness in-process; a repo without that script is not enforced.
    """
    root = Path(root)

    def changed(common_dir: Path) -> Dict[str, Optional[int]]:
        out: Dict[str, Optional[int]] = {}
        for path, entry in assembled.items():
            if not path.startswith(_SKILLS_PREFIX):
                continue
            if isinstance(entry, tuple) and len(entry) == 2 and entry[0] != 0o160000:
                blob = read_object(common_dir, entry[1])
                if blob is None or blob[0] != "blob":
                    raise ValueError(f"blob for {path} unreadable")
                out[path] = len(blob[1])
            else:
                out[path] = None
        return out

    def baseline_text(common_dir: Path) -> Optional[str]:
        entry = assembled.get(_SW_BASELINE)
        if not (isinstance(entry, tuple) and len(entry) == 2):
            entry = head_blobs(root, [_SW_BASELINE]).get(_SW_BASELINE)
        return None if entry is None else _blob_text(common_dir, entry[1])

    return _skill_weight_leg(root, assembled, changed, baseline_text)


def skill_weight_check_worktree(
    root: Path, paths: Iterable[str], deleted_paths: Iterable[str] = ()
) -> Tuple[Optional[str], Optional[str]]:
    """`skill_weight_check` for a commit that has no assembled tree yet: sizes
    come from the named worktree files, sized as the blob `git add` would
    write (`checkin_sizes`), not by raw worktree length. `commit_v2` lands through
    `commit_paths`, which never reaches `_commit_via_head_spine`."""
    root = Path(root)
    named = [*paths, *deleted_paths]

    def changed(common_dir: Path) -> Dict[str, Optional[int]]:
        files: List[str] = []
        for path in named:
            if not path.startswith(_SKILLS_PREFIX):
                continue
            live = root / path
            if live.is_dir():
                files.extend(
                    f.relative_to(root).as_posix() for f in live.rglob("*") if f.is_file()
                )
            else:
                files.append(path)
        return checkin_sizes(
            root,
            list(dict.fromkeys(files)),
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=root),
        )

    def baseline_text(common_dir: Path) -> Optional[str]:
        live = root / _SW_BASELINE
        if _SW_BASELINE in named and live.is_file():
            return live.read_bytes().decode("utf-8", errors="replace")
        entry = head_blobs(root, [_SW_BASELINE]).get(_SW_BASELINE)
        return None if entry is None else _blob_text(common_dir, entry[1])

    return _skill_weight_leg(root, named, changed, baseline_text)


def _skill_weight_leg(root, touched, changed, baseline_text_for):
    admission_path = root / _SW_ADMISSION
    if not admission_path.is_file():
        return None, None
    if not any(
        p.startswith(_SKILLS_PREFIX) or p in (_SW_BASELINE, _SW_ADMISSION) for p in touched
    ):
        return None, None

    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    try:
        common_dir = resolve_git_common_dir(root)
        base_sizes = _head_skill_sizes(root, common_dir)
        proposed_sizes = dict(base_sizes)
        for path, size in changed(common_dir).items():
            if size is None:
                proposed_sizes.pop(path, None)
            else:
                proposed_sizes[path] = size

        text = baseline_text_for(common_dir)
        if text is None:
            raise ValueError("skill-weight baseline absent or unreadable")
        baseline = json.loads(text)

        admission = _load_by_path("_claude_klabauter_skill_weight_admission", admission_path)
        harness = _load_by_path("_claude_klabauter_skill_weight_harness", root / _SW_HARNESS)

        def resident(sizes: Dict[str, int]) -> Dict[str, int]:
            measured = harness.measure_all_skills(sizes=sizes)
            return {name: row["resident"] for name, row in measured.items()}

        violations = admission.admission_violations(
            resident(proposed_sizes), resident(base_sizes), baseline
        )
    except Exception as exc:  # noqa: BLE001 -- fail open, see module docstring.
        _LOG.warning("%s did not run this commit: %s", _SW_TAG, exc)
        return None, f"{_SW_TAG} check did not run: {exc.__class__.__name__}: {exc}"
    finally:
        sys.path[:] = saved_path
        for key in set(sys.modules) - saved_modules:
            sys.modules.pop(key, None)

    if not violations:
        return None, None
    return (
        "\n".join(
            [f"{_SW_TAG} commit refused: skill growth over budget", *(f"  {v}" for v in violations)]
        ),
        None,
    )


def _incomplete(path: str, exc: Exception) -> str:
    return (
        f"{path}: the doctrine-surface admission check could not complete "
        f"({exc.__class__.__name__}: {exc}) -- refusing rather than landing an "
        "unchecked change to a governed doctrine surface."
    )


def governed_write_refusal(root: Path, path: str, old_text: str, new_text: str) -> Optional[str]:
    root = Path(root)
    if path not in GOVERNED_AUTHORING_SURFACES or not resolve_ledger_path(root, path).is_file():
        return None
    try:
        allowed, message = admission_check_for_surface(path, old_text, new_text, root)
    except Exception as exc:  # noqa: BLE001 -- fail closed, see module docstring.
        return _incomplete(path, exc)
    return None if allowed else f"{path}: {message}"


def uncommitted_surface_refusals(root: Path) -> List[str]:
    root = Path(root)
    governed = [p for p in GOVERNED_AUTHORING_SURFACES if resolve_ledger_path(root, p).is_file()]
    if not governed:
        return []
    common_dir = resolve_git_common_dir(root)
    old_blobs = head_blobs(root, governed)
    refusals: List[str] = []
    for path in governed:
        live = root / path
        if not live.is_file():
            continue
        try:
            # `.replace("\r\n", "\n")` -- a git blob is LF-normalized
            # content (git's own storage convention); the WORKING-TREE
            # read is not, and on a Windows checkout with the common
            # `core.autocrlf=true` default, git rewrites every LF back to
            # CRLF on checkout. Comparing the two unnormalized would read
            # that checkout-time rewrite alone as "every line grew" on an
            # otherwise byte-identical, untouched file -- a false refusal
            # this fleet's "Windows is first-class" bar does not allow
            # (CLAUDE.md's Runtime conventions). `commit_authored_content`'s
            # own callers pass content in-process, never round-tripping a
            # checkout, so this is the one read site that needs it.
            new_text = live.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
            old_entry = old_blobs.get(path)
            old_text = "" if old_entry is None else _blob_text(common_dir, old_entry[1])
        except Exception as exc:  # noqa: BLE001 -- fail closed, see module docstring.
            refusals.append(_incomplete(path, exc))
            continue
        if old_text is None:
            refusals.append(f"{path}: its HEAD blob could not be read, so its admission could not be checked.")
            continue
        if new_text == old_text:
            continue
        refusal = governed_write_refusal(root, path, old_text, new_text)
        if refusal is not None:
            refusals.append(refusal)
    return refusals
