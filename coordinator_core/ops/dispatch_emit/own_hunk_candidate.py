"""A run's landing candidate for one path: HEAD plus only the hunks the run recorded or declared.

`candidate(repo_root, run_id, path, head_bytes, tree_bytes)` diffs the tree against HEAD and classifies each
hunk. Own: its exact (removed, added) block is in a blob the run recorded for `path` (`kept.json`, the
halt's `touched-blobs.json`), or the run declares `path` in its manifest and no other run recorded the
block. A block the run recorded is own even when a peer recorded it too. Foreign: any other hunk, named by
the other run that recorded it, else `unknown`. Foreign hunks are excluded from `bytes`; a path whose delta
cannot be classified is refused. Zero spawn, never raises; `head_bytes` and `tree_bytes` are check-in form.

`close_over(root, paths, withheld, ...)` is the path-level closure the checkpoint, halt and terminal commits
share: the further staged paths that must leave because a withheld path's definition or module left.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

UNKNOWN_OWNER = "unknown"
COARSE = "coarse"
NO_EVIDENCE = "no-evidence"
TEST_OF_WITHHELD_REASON = "test of a row whose code is withheld: the row lands whole or waits"
CONSUMER_REASON = "consumer of a withheld producer: {why}"


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def _stem_tests(paths: Sequence[str], held: set) -> set:
    """Staged test paths named `test_<stem>.py`, beside or under `tests/` of a withheld `<stem>.py`."""
    from coordinator_core.git.checkpoint_hunks import _is_test_path

    want: set = set()
    for h in held:
        if h.endswith(".py") and not _is_test_path(h):
            parent, _, name = h.rpartition("/")
            for d in (parent, f"{parent}/tests" if parent else "tests"):
                want.add(f"{d}/test_{name}" if d else f"test_{name}")
    return {p for p in paths if p not in held and p in want}


def close_over(
    root: Path, paths: Sequence[str], withheld: Sequence[str], blobs: Optional[dict] = None,
    message: str = "", *, pair_by_stem: bool = True,
) -> dict:
    """`{path: reason}` for each staged path in `paths` that must leave with `withheld`: a test of a
    withheld module (a plan row's test when `message` names the plan; also `test_<stem>.py` beside or
    under `tests/` of a withheld `<stem>.py` unless `pair_by_stem` is False) and a consumer of a
    definition that exists only in a withheld path. A path that leaves is itself withheld for the next
    pass, so the result is a fixpoint. `blobs` overrides a staged path's worktree content. Zero git
    spawns; never raises."""
    found: dict = {}
    try:
        from coordinator_core.git import checkpoint_hunks as ch

        root = Path(root)
        norm = [_norm(p) for p in paths]
        held = {_norm(p) for p in withheld}
        while True:
            fresh: dict = {}
            tests = set(ch.tests_of_withheld_code(root, message, norm, sorted(held))) if message else set()
            if pair_by_stem:
                tests |= _stem_tests(norm, held)
            for p in sorted(tests - held):
                fresh[p] = TEST_OF_WITHHELD_REASON
            consumers = ch.consumers_of_withheld_definitions(root, norm, sorted(held | set(fresh)), blobs)
            for p, why in consumers.items():
                if _norm(p) not in held and _norm(p) not in fresh:
                    fresh[_norm(p)] = CONSUMER_REASON.format(why=why)
            if not fresh:
                return found
            found.update(fresh)
            held |= set(fresh)
    except Exception:  # noqa: BLE001 -- unjudgeable: keep what earlier passes already withheld
        return found


def _key(blob: bytes) -> list:
    return [ln.rstrip(b"\r\n") for ln in blob.splitlines(True)]


def _lines(first: int, last: int, deletion: bool) -> str:
    if deletion:
        return f"{first} (deletion)"
    return f"{first}-{last}" if last > first else str(first)


def _blocks(root: Path, common: Path, path: str, head_keys: list, shas) -> set:
    """`{(removed, added)}` blocks of each recorded blob in `shas` against HEAD; an unreadable or
    unliftable blob contributes nothing."""
    from coordinator_core.git import git_objects
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    out: set = set()
    for sha in shas:
        obj = git_objects.read_object(common, sha)
        lifted = kept_work.checkin_form(root, path, obj[1]) if obj is not None and obj[0] == "blob" else None
        if lifted is None:
            continue
        keys = _key(lifted)
        for _tag, i1, i2, j1, j2 in rhc.changed_opcodes(head_keys, keys):
            out.add((tuple(head_keys[i1:i2]), tuple(keys[j1:j2])))
    return out


def _recorded(record_dir: Path, path: str) -> set:
    """Blob shas a run's record names for `path`: `kept.json` blobs and the `touched-blobs.json` run blob."""
    from coordinator_core.ops.dispatch_emit import kept_work, stranded_sibling

    shas = set(kept_work.kept_fingerprints(record_dir).get(path, ()))
    run_blob = stranded_sibling.read(record_dir).get(path, {}).get("run")
    if run_blob:
        shas.add(run_blob)
    return shas


def _peer_owners(root: Path, common: Path, run_id: str, path: str, head_keys: list) -> dict:
    """`{block: run id}` over the most recent other runs' recorded blobs for `path`
    (`stranded_sibling.recent_run_dirs` bounds the runs)."""
    from coordinator_core.ops.dispatch_emit import stranded_sibling
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    owners: dict = {}
    for name, rdir in stranded_sibling.recent_run_dirs(Path(root) / RECORD_DIR_ROOT, run_id):
        shas = _recorded(rdir, path)
        if shas:
            for block in _blocks(root, common, path, head_keys, sorted(shas)):
                owners.setdefault(block, name)
    return owners


def _declares(root: Path, run_id: str, path: str) -> bool:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.halted_record_request import _manifest_doc
    from coordinator_core.ops.dispatch_emit.stranded_sibling import _declared_paths

    return path in _declared_paths(_manifest_doc(record_dir(root, run_id)))


def candidate(
    repo_root: Path, run_id: str, path: str, head_bytes: Optional[bytes], tree_bytes: bytes,
    *, declared: Optional[bool] = None,
) -> dict:
    """`{"bytes", "excluded": [{"lines", "owner"}]}` when `path` can be landed as HEAD plus the run's own
    hunks (`excluded` empty: the tree is the run's own and `bytes` is `tree_bytes`), else
    `{"refused": reason, "lines", "owner"}` naming the first hunk it could not attribute and a `foreign`
    list of every one. `lines` are tree-side ranges. `declared` overrides the manifest read: True lets the
    run own every hunk no other run recorded, False requires a recorded block for every own hunk."""
    try:
        return _candidate(Path(repo_root), run_id, path, head_bytes or b"", tree_bytes, declared)
    except Exception as exc:  # noqa: BLE001 -- unknown is refused, never landed
        return {"refused": NO_EVIDENCE, "lines": "all", "owner": UNKNOWN_OWNER, "foreign": [], "error": str(exc)}


def _candidate(root: Path, run_id: str, path: str, head: bytes, tree: bytes, declared: Optional[bool]) -> dict:
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    head_keys, tree_keys = _key(head), _key(tree)
    hunks = rhc.changed_opcodes(head_keys, tree_keys)
    if not hunks:
        return {"bytes": tree, "excluded": []}
    if rhc.exceeds_match_bound(head_keys, tree_keys):
        return {"refused": COARSE, "lines": "all", "owner": UNKNOWN_OWNER, "foreign": []}
    common = resolve_git_common_dir(root)
    own_blocks = _blocks(root, common, path, head_keys, sorted(_recorded(record_dir(root, run_id), path)))
    owns_path = declared if declared is not None else _declares(root, run_id, path)
    peers = _peer_owners(root, common, run_id, path, head_keys)

    h_lines, t_lines = head.splitlines(True), tree.splitlines(True)
    out: list = []
    excluded: list = []
    pos = 0
    taken = 0
    for _tag, i1, i2, j1, j2 in hunks:
        block = (tuple(head_keys[i1:i2]), tuple(tree_keys[j1:j2]))
        out += h_lines[pos:i1]
        if block in own_blocks or (owns_path and block not in peers):
            out += t_lines[j1:j2]
            taken += 1
        else:
            out += h_lines[i1:i2]
            excluded.append({"lines": _lines(j1 + 1, j2, j2 == j1), "owner": peers.get(block, UNKNOWN_OWNER)})
        pos = i2
    out += h_lines[pos:]
    if not excluded:
        return {"bytes": tree, "excluded": []}
    if not taken:
        first = excluded[0]
        return {"refused": NO_EVIDENCE, "lines": first["lines"], "owner": first["owner"], "foreign": excluded}
    return {"bytes": b"".join(out), "excluded": excluded}
