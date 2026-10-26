"""``dispatch.land_combined_index``: one commit of the staged index, crediting the halted runs it carries.

Params: ``paths`` (list of repo-relative strings: the declared set), ``run_ids`` (list of halted run
ids to credit), ``message`` (string: the commit message). The op never stages: the index is its
input and the working tree belongs to its peers. It refuses unless ``git diff --cached --name-only``
equals the declared set, then commits the index tree (``write-tree``, ``commit-tree``,
``update-ref`` with the old HEAD as compare value), so a peer's unstaged hunk on a declared file stays
in the working tree. A declared path another halted run holds (``shared_path_landing.holds_path``)
refuses the landing unless the committed blob is byte-equal to HEAD plus the credited runs' kept work and
the working-tree residue 3-way-applies onto it (git's own ``apply --3way``); a holder whose record carries
withheld-prints refuses whole (DR-462). The hunk check adds three spawns per landing (``cat-file --batch``,
``diff``, ``apply``), none when nothing is held; the spawn bound is at most three per landing.
A credited run whose withheld set lies wholly inside the declared set is recorded landed at the new
commit through ``halted_run_landed._close_landed`` only when every withheld path also passes
``halted_hold_release.kept_at_head_paths`` against the new HEAD; any other credited run stays ``halted`` and is
reported under ``partial`` with the withheld paths the commit did not carry or keep.
Reply: ``{ok, sha, committed, landed, partial}`` or ``{ok: false, error}``. Git spawns: ``diff --cached``,
``write-tree``, ``commit-tree``, ``update-ref`` (plus HEAD reads, one kept-blob batch per fully-carried run, and whatever ``_close_landed`` spawns per credited run
and ``holds_path`` spawns per peer holder and declared path; no ``release_scope`` shares those, and the tests stub ``_launch``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.land_combined_index"


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _norm(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _str_list(value) -> Optional[list]:
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v for v in value):
        return None
    return list(dict.fromkeys(value))


def _git(root: Path, *args: str, stdin: Optional[bytes] = None):
    from coordinator_core.git.run import run_git

    return run_git(["-C", str(root), *args], input=stdin)


def _staged_set(root: Path) -> tuple[Optional[set], Optional[str]]:
    done = _git(root, "diff", "--cached", "--name-only", "-z")
    if not done.ok:
        return None, f"git diff --cached failed: {done.stderr.strip() or done.returncode}"
    names = [_norm(n) for n in done.stdout.split("\0") if n]
    return set(names), None


def _credited_kept(root: Path, credited: set) -> dict:
    """``{path: [(kept blob spec, run_base_sha or "")]}`` over the credited runs' kept work, in run-id order.
    A run whose kept record is unreadable or malformed contributes nothing, so its paths stay held."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_hold_release, halted_run
    from coordinator_core.ops._path_guard import safe_id

    out: dict = {}
    for run_id in sorted(credited):
        if not safe_id(run_id):
            continue
        try:
            record = ask_contract.record_dir(root, run_id)
            plan = halted_hold_release._kept_plan(record)
            base = halted_hold_release._base_sha(halted_run._read(record) or {})
        except Exception:  # noqa: BLE001 -- unreadable or malformed kept work: the run's paths stay held
            continue
        if plan is not None:
            for path in plan[0]:
                out.setdefault(path, []).append((plan[1][path], base))
    return out


def _expected_blob(path: str, head: bytes, keeps: list, blobs: dict) -> Optional[bytes]:
    """HEAD's blob with the credited runs' kept work applied in run-id order, or None when it cannot be built
    exactly. A run applies only when its base blob for *path* is byte-equal to the blob built so far, so the
    result is that run's kept blob; a run with no recorded base ties nothing to HEAD and returns None."""
    built = head
    for spec, base in keeps:
        kept = blobs.get(spec)
        if kept is None or not base:
            return None
        base_blob = blobs.get(f"{base}:{path}")
        if base_blob is None or base_blob != built:
            return None
        built = kept
    return built


def _residue_conflicts(root: Path, candidates: list) -> set:
    """The *candidates* whose working tree differs from HEAD in a way that does not 3-way-apply onto the index
    blob. ``git apply --cached --3way --check`` of the HEAD-to-working-tree patch is git's own merge of
    (base HEAD, ours index, theirs working tree); two spawns for the whole candidate list. A non-zero apply
    exit holds every candidate. Under ``--check`` a merge conflict exits 0 and says "Applied patch to '<path>'
    with conflicts" on stderr, so for an exit-0 apply stderr names the conflicted subset; the apply runs under ``LC_ALL=C`` with ``LANGUAGE`` unset so that text is never translated. An
    empty diff means the working tree carries no residue over HEAD for any candidate."""
    from coordinator_core.git.run import run_git

    if not candidates:
        return set()
    env = {k: v for k, v in os.environ.items() if k not in ("LANGUAGE", "LC_ALL")}
    env["LC_ALL"] = "C"
    try:
        diff = run_git(
            ["--no-optional-locks", "--literal-pathspecs", "-C", str(root), "diff", "--binary", "--full-index",
             "--no-ext-diff", "--no-textconv", "HEAD", "--", *candidates],
            binary=True,
        )
        if not diff.ok:
            return set(candidates)
        if not diff.stdout_bytes.strip():
            return set()
        applied = run_git(
            ["-C", str(root), "apply", "--cached", "--3way", "--check"], input=diff.stdout_bytes,
            env=env,
        )
    except OSError:
        return set(candidates)
    if not applied.ok:
        return set(candidates)
    conflicted = [ln for ln in applied.stderr.splitlines() if "with conflicts" in ln]
    if not conflicted:
        return set()
    named = {p for p in candidates if any(f"'{p}'" in ln for ln in conflicted)}
    return named or set(candidates)


def _hunk_aware_clear(root: Path, paths: list, credited: set) -> set:
    """The *paths* whose committed (index) blob is byte-equal to HEAD plus the credited runs' kept work and
    whose working-tree residue is empty or 3-way-applies onto it (``_residue_conflicts``). Three git spawns for
    the whole list (``cat-file --batch``, ``diff``, ``apply``); any unreadable input leaves the path held."""
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    paths = [p for p in paths if "\n" not in p and "\r" not in p]
    kept = _credited_kept(root, credited)
    specs = list(dict.fromkeys(
        [f"HEAD:{p}" for p in paths] + [f":{p}" for p in paths]
        + [s for p in paths for k, b in kept.get(p, ()) for s in ((k, f"{b}:{p}") if b else (k,))]
    ))
    blobs = halted_hold_release._blob_batch(root, specs)
    if blobs is None:
        return set()
    candidates: list = []
    for path in paths:
        head_blob, committed_blob = blobs.get(f"HEAD:{path}"), blobs.get(f":{path}")
        if not kept.get(path) or head_blob is None or committed_blob is None:
            continue
        expected = _expected_blob(path, head_blob, kept[path], blobs)
        if expected is None or expected == head_blob or expected != committed_blob:
            continue
        candidates.append(path)
    return set(candidates) - _residue_conflicts(root, candidates)


def _peer_holder(root: Path, declared: set, credited: set) -> Optional[tuple[str, str]]:
    """``(path, holder)`` of the first declared path a halted run outside *credited* still holds.

    A held path stays refused when its holder's record carries withheld-prints. Otherwise it is released
    when the committed blob is HEAD plus the credited runs' hunks only and the working-tree residue
    leaves those hunks intact (``_hunk_aware_clear``)."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_withheld_index as idx
    from coordinator_core.ops.dispatch_emit.shared_path_landing import holds_path
    from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE

    idx.withheld_by_path(root)
    try:
        names = sorted(n for n in os.listdir(idx._dir(root)) if n.endswith(".json"))
    except OSError:
        return None
    held: dict = {}
    for name in names:
        holder = name[: -len(".json")]
        if holder in credited:
            continue
        for path in idx._paths_of(idx._dir(root) / name):
            if path in declared and holds_path(root, holder, path, None):
                try:
                    prints = (ask_contract.record_dir(root, holder) / WITHHELD_PRINTS_FILE).exists()
                except (OSError, ValueError):
                    prints = True
                if prints:
                    return path, holder
                held.setdefault(path, holder)
    if not held:
        return None
    clear = _hunk_aware_clear(root, sorted(held), credited)
    for path in sorted(held):
        if path not in clear:
            return path, held[path]
    return None


def _classify(root: Path, run_id: str, declared: set) -> tuple[Optional[str], list]:
    """``(refusal, missing)``: *missing* is the credited run's withheld paths outside *declared*."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
    from coordinator_core.ops.dispatch_emit.halted_run_landed import _live_successor

    if not safe_id(run_id):
        return f"{run_id} is not a safe run id", []
    try:
        record_dir = ask_contract.record_dir(root, run_id)
        marker = halted_run.read_strict_run(record_dir)
        withheld = sorted({_norm(p) for p in halted_run.halted_withheld_strict(record_dir)})
    except halted_run.UnreadableHaltRecord as exc:
        return f"{exc.reason}: {run_id} halt marker unreadable", []
    except ValueError as exc:
        return str(exc), []
    if marker is None:
        return f"{run_id} has no halt record", []
    status = marker.get("status")
    if status not in (halted_run.STATUS_HALTED, halted_run.STATUS_RESUMED):
        return f"{run_id} is {status}; only a halted run is credited", []
    if status == halted_run.STATUS_RESUMED:
        successor = _live_successor(root, run_id)
        if successor:
            return f"{run_id} is resumed and its successor {successor} is still live", []
    if not withheld:
        return f"{run_id} withholds no paths; a combined landing credits only a run whose withheld paths it carries", []
    return None, [p for p in withheld if p not in declared]


def _unkept_at_head(root: Path, run_id: str) -> list:
    """The run's withheld paths whose kept content is not at the new HEAD (blob-equal or kept diff present);
    a path with no kept blob counts as unkept, so an override that dropped the run's hunk never credits it."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_hold_release, halted_run

    record_dir = ask_contract.record_dir(root, run_id)
    marker = halted_run._read(record_dir) or {}
    withheld = {_norm(p) for p in halted_run.halted_withheld_strict(record_dir)}
    return sorted(withheld - halted_hold_release.kept_at_head_paths(root, record_dir, marker))


def _commit_index(root: Path, message: str, expected_head: str) -> tuple[Optional[str], Optional[str]]:
    """Commit the index tree on *expected_head*, the HEAD read before the staged set was verified: a HEAD that
    moved since would make the commit revert the peer's commit on every undeclared file."""
    from coordinator_core.git.git_state import head_sha

    head = head_sha(root, fresh=True)
    if not head:
        return None, "HEAD is unreadable; retry"
    if head != expected_head:
        return None, "HEAD moved after the staged set was verified; nothing committed; retry"
    tree = _git(root, "write-tree")
    if not tree.ok:
        return None, f"git write-tree failed: {tree.stderr.strip() or tree.returncode}"
    made = _git(root, "commit-tree", tree.stdout.strip(), "-p", head, "-m", message)
    if not made.ok:
        return None, f"git commit-tree failed: {made.stderr.strip() or made.returncode}"
    sha = made.stdout.strip()
    moved = _git(root, "update-ref", "-m", f"commit: {message.splitlines()[0]}", "HEAD", sha, head)
    if not moved.ok:
        return None, f"HEAD moved under the landing; nothing recorded: {moved.stderr.strip() or moved.returncode}"
    return sha, None


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.land_combined_index": verify the staged set, commit the index, credit the runs."""
    from coordinator_core.ops.dispatch_emit.halted_run_landed import _close_landed

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    paths = _str_list(params.get("paths"))
    run_ids = _str_list(params.get("run_ids"))
    message = params.get("message")
    if paths is None or run_ids is None or not isinstance(message, str) or not message.strip():
        return _refuse(f"{OP_NAME} requires non-empty paths, run_ids and message")
    declared = {_norm(p) for p in paths}
    root = main_worktree_root(Path(repo_root))

    from coordinator_core.git.git_state import head_sha

    expected_head = head_sha(root, fresh=True)
    if not expected_head:
        return _refuse("HEAD is unreadable; retry")
    staged, problem = _staged_set(root)
    if problem:
        return _refuse(problem)
    if staged != declared:
        extra, absent = sorted(staged - declared), sorted(declared - staged)
        return _refuse(
            f"staged set differs from declared: staged-not-declared {extra[:5]}, declared-not-staged {absent[:5]}"
        )
    held = _peer_holder(root, declared, set(run_ids))
    if held:
        return _refuse(f"{held[0]} is held by halted run {held[1]}; it lands whole with that run or not at all")
    plan: dict = {}
    for run_id in run_ids:
        problem, missing = _classify(root, run_id, declared)
        if problem:
            return _refuse(problem)
        plan[run_id] = missing

    sha, problem = _commit_index(root, message, expected_head)
    if problem:
        return _refuse(problem)
    landed, partial = [], []
    for run_id, missing in plan.items():
        if not missing:
            missing = _unkept_at_head(root, run_id)
        if missing:
            partial.append({"run_id": run_id, "missing": missing})
        else:
            landed.append({"run_id": run_id, **_close_landed(root, run_id, sha, None)})
    return {"ok": True, "sha": sha, "committed": sorted(declared), "landed": landed, "partial": partial}
