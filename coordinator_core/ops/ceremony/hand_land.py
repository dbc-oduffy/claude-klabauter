"""``ceremony.hand_land``: land named paths on HEAD through ``commit_paths``, so a hand-land cannot skip the worktree sync.

Params: ``repo_root`` (string, optional: the repo to land in; defaults to the dispatching worktree's main root),
``paths`` (list of repo-relative path strings, required), ``message`` (string, required; the op appends a
``Hand-Landed: true`` trailer), ``expect_head`` (string, optional: a full or abbreviated HEAD sha; a HEAD that
does not start with it refuses with nothing moved), ``run_id`` (string, optional: a known run adds one
``Governed-By`` trailer per committed plan or sizing file its record names).

The commit, the ref CAS and the 3-way sync of each landed path into the worktree are ``commit_paths``'s; this op
adds no commit-tree or update-ref of its own. A keyed dispatch also gets the durable land record ``land_run``
writes, advanced ``committing`` then ``committed``.

Before any commit the op refuses, with nothing moved, ``raw_kept_blob`` (a path whose worktree bytes are a halted
run's raw ``kept.json`` image while its ``withheld-own-delta.json`` holds a different own-delta blob) and
``uncommitted_import`` (a landing ``.py`` importing a ``coordinator_core`` name that resolves only uncommitted).

Reply: ``{ok, sha, rebased, sync_refused}``; a non-empty ``sync_refused`` adds ``sync_incomplete: true`` and
``worktree_sync_refused`` (the landing stands, those paths stay byte-intact); ``ownerless`` names landed paths no run
manifest declares (advisory); ``gate_unverified`` names a pre-commit gate that could not run and so allowed
(advisory); ``stranded`` names ``[{path, kind, reason, run_id}]``, the recorded sibling paths of a
halted run (``touched-blobs.json``) the landing leaves behind (advisory: the landing stands). Refusal:
``{ok: false, error, refused}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

OP_NAME = "ceremony.hand_land"
TRAILER = "Hand-Landed: true"


def _refuse(error: str, kind: str) -> dict:
    return {"ok": False, "error": error, "refused": kind}


def _root(params: dict, repo_root: Optional[Path]) -> Optional[Path]:
    given = params.get("repo_root")
    if isinstance(given, str) and given:
        return Path(given)
    if repo_root is None:
        return None
    from coordinator_core.lifecycle import main_worktree_root

    return main_worktree_root(repo_root)


def _stranded_siblings(root: Path, paths: list) -> list:
    """Recorded siblings of a halted run that landing ``paths`` would leave behind; one read of each record in
    ``state/warp`` plus one batched HEAD read over the intersecting records' paths. ``[]`` on any error."""
    try:
        from coordinator_core.authoring_leaks import HeadView
        from coordinator_core.ops.dispatch_emit import stranded_sibling

        from coordinator_core.session import record_homes

        warp = Path(record_homes.home_dir(str(root), "warp"))
        landing = {p.replace("\\", "/").removeprefix("./") for p in paths}
        records = stranded_sibling.read_records(warp)
        involved: set = set()
        for _run, _dir, _mtime, rec in records:
            if landing.intersection(rec):
                involved.update(rec)
        if not involved:
            return []
        head = HeadView(root, involved)
        head_blobs = {p: (head.entry(p) or (0, None))[1] for p in involved}
        return stranded_sibling.stranded(root, warp, landing, head_blobs, records)
    except Exception:  # noqa: BLE001 -- an unreadable record is no claim, never a refusal
        return []


def _raw_kept_blobs(root: Path, paths: list) -> Optional[list]:
    """`[{path, run_id, blob}]` for each landing path whose worktree bytes are a halted run's raw ``kept.json``
    image (peer dirt included) while the run's record holds an own-delta blob (or names the path unlandable) that
    differs from it. One stat per recent run dir; only a dir holding ``withheld-own-delta.json`` is read. ``None``
    when the gate could not run."""
    try:
        import json

        from coordinator_core.git.commit import _checkin_bytes
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_objects import read_object
        from coordinator_core.ops.dispatch_emit import kept_work, stranded_sibling
        from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE
        from coordinator_core.session import record_homes

        landing = {p.replace("\\", "/").removeprefix("./") for p in paths}
        warp = Path(record_homes.home_dir(str(root), "warp"))
        common = resolve_git_common_dir(root)
        found: list = []
        for run_id, rec_dir in stranded_sibling.recent_run_dirs(warp):
            own_file = rec_dir / WITHHELD_OWN_DELTA_FILE
            if not own_file.is_file():
                continue
            raw_by_path = kept_work.kept_fingerprints(rec_dir)
            hits = landing.intersection(raw_by_path)
            if not hits:
                continue
            own = json.loads(own_file.read_text(encoding="utf-8"))
            deltas = own.get("paths") or {}
            unlandable = set(own.get("unlandable") or ())
            for p in sorted(hits):
                if kept_work.fingerprint(root, p) not in raw_by_path[p]:
                    continue
                blob = (deltas.get(p) or {}).get("blob")
                if blob is None and p not in unlandable:
                    continue
                if blob is not None:
                    got = read_object(common, blob)
                    worktree = (root / p).read_bytes()
                    if got is not None and got[1] == _checkin_bytes(root, p, worktree):
                        continue
                found.append({"path": p, "run_id": run_id, "blob": blob})
        return found
    except Exception:  # noqa: BLE001 -- an unreadable record is no claim, never a refusal; the caller says so
        return None


def _uncommitted_only_imports(root: Path, paths: list) -> Optional[list]:
    """`['<path>: imports <name>', ...]` for each module-level ``coordinator_core.*`` import (unguarded,
    ``percolate.guards`` scope) in a landing ``.py`` path that resolves in the worktree but not in HEAD plus the
    landing paths: the publish's from-import resolution check on the resulting tree, zero spawns. ``None`` when the
    gate could not run."""
    try:
        import ast
        import tempfile

        from coordinator_core.authoring_leaks import HeadView
        from coordinator_core.percolate.guards import (
            _COORDINATOR_CORE_IMPORT_PREFIX,
            _absent_from_import_names,
            _import_target_present,
            _module_level_import_targets,
        )

        landing = sorted({p.replace("\\", "/").removeprefix("./") for p in paths})
        imports: list = []
        for p in landing:
            if not p.endswith(".py"):
                continue
            try:
                tree = ast.parse((root / p).read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            surface = "tests" in Path(p).parts or Path(p).name == "conftest.py"
            for stmt, guarded in _module_level_import_targets(tree, test_surface=surface):
                if guarded or (isinstance(stmt, ast.ImportFrom) and stmt.level):
                    continue
                mods = [stmt.module] if isinstance(stmt, ast.ImportFrom) else [a.name for a in stmt.names]
                imports.extend((p, stmt, m) for m in mods if m and m.startswith(_COORDINATOR_CORE_IMPORT_PREFIX))
        if not imports:
            return []

        candidates: set = set()
        for _p, stmt, mod in imports:
            base = mod.replace(".", "/")
            candidates.update((f"{base}.py", f"{base}/__init__.py"))
            if isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    candidates.update((f"{base}/{alias.name}.py", f"{base}/{alias.name}/__init__.py"))
        head = HeadView(root, candidates)
        refused: list = []
        with tempfile.TemporaryDirectory(prefix="hand-land-tree-") as tmp:
            result = Path(tmp)
            for rel in sorted(candidates | set(landing)):
                if rel in landing and (root / rel).is_file():
                    data = (root / rel).read_bytes()
                elif head.in_head(rel):
                    data = head.head_bytes(rel) or b""
                else:
                    continue
                (result / rel).parent.mkdir(parents=True, exist_ok=True)
                (result / rel).write_bytes(data)
            for _p, _stmt, mod in imports:
                parts = mod.split(".")
                if head.dir_entries("/".join(parts)):
                    result.joinpath(*parts).mkdir(parents=True, exist_ok=True)
            exports_result: dict = {}
            exports_worktree: dict = {}
            for p, stmt, mod in imports:
                if not _import_target_present(mod, result):
                    if _import_target_present(mod, root):
                        refused.append(f"{p}: imports {mod!r}, which exists only uncommitted")
                    continue
                if isinstance(stmt, ast.ImportFrom):
                    missing = set(_absent_from_import_names(stmt, mod, result, exports_result))
                    present = missing - set(_absent_from_import_names(stmt, mod, root, exports_worktree))
                    refused.extend(f"{p}: imports {mod + '.' + n!r}, which exists only uncommitted" for n in sorted(present))
        return sorted(set(refused))
    except Exception:  # noqa: BLE001 -- an unreadable tree is no claim, never a refusal; the caller says so
        return None


def _uncommitted_attr_names(root: Path, paths: list) -> Optional[list]:
    """`landed_names` refusals (`alias.attr`, literal `getattr`) for the landing ``.py`` paths, judged on the tree
    the land produces: landing paths read from the worktree, every other path from HEAD (zero spawns). Module
    paths outside the import spine of the landing files read as absent, so the check under-refuses, never
    false-refuses. ``None`` when the check could not run."""
    try:
        import ast

        from coordinator_core.authoring_leaks import HeadView
        from coordinator_core.ops.ceremony.landed_names import uncommitted_name_refusals

        landing = sorted({p.replace("\\", "/").removeprefix("./") for p in paths})
        py = [p for p in landing if p.endswith(".py")]
        candidates: set = set()
        for p in py:
            try:
                tree = ast.parse((root / p).read_bytes())
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    mods = [(node.module, [a.name for a in node.names])]
                elif isinstance(node, ast.Import):
                    mods = [(a.name, []) for a in node.names]
                for mod, names in mods:
                    parts = mod.split(".")
                    for i in range(1, len(parts) + 1):
                        base = "/".join(parts[:i])
                        candidates.update((f"{base}.py", f"{base}/__init__.py"))
                    base = "/".join(parts)
                    for n in names:
                        candidates.update((f"{base}/{n}.py", f"{base}/{n}/__init__.py"))
        head = HeadView(root, candidates)
        landing_set = set(landing)

        def read(rel: str) -> Optional[bytes]:
            if rel in landing_set:
                try:
                    return (root / rel).read_bytes()
                except OSError:
                    return None
            return head.head_bytes(rel)

        return uncommitted_name_refusals(root, py, read)
    except Exception:  # noqa: BLE001 -- an unreadable tree is no claim, never a refusal; the caller says so
        return None


def hand_land(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land ``params['paths']`` with ``params['message']`` on HEAD; see the module docstring for the reply."""
    from coordinator_core.git import git_state
    from coordinator_core.git.commit import CommitRefused, commit_paths, sync_report
    from coordinator_core.git.ref_cas_retry import REF_CAS_ATTEMPTS, is_ref_moved
    from coordinator_core.ops.dispatch_emit import land_intent

    root = _root(params, repo_root)
    if root is None:
        return _refuse("repo_root is required", "params")
    paths = params.get("paths")
    message = params.get("message")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        return _refuse("paths must be a non-empty list of path strings", "params")
    if not isinstance(message, str) or not message.strip():
        return _refuse("message must be a non-empty string", "params")
    expect = params.get("expect_head")
    if expect is not None and (not isinstance(expect, str) or not expect):
        return _refuse("expect_head must be a non-empty sha string", "params")

    head = git_state.head_sha(root, fresh=True)
    if expect is not None and not (head or "").startswith(expect.lower()):
        return _refuse(f"HEAD is {head}, expected {expect}: nothing moved", "stale_head")

    gate_unverified: list = []
    raw = _raw_kept_blobs(root, paths)
    if raw is None:
        gate_unverified.append("raw_kept_blob")
    elif raw:
        named = "; ".join(
            f"{r['path']} (run {r['run_id']}: " + (f"own-delta blob {r['blob']}" if r["blob"] else "unlandable, no own-delta") + ")"
            for r in raw
        )
        return _refuse(
            f"worktree bytes are a halted run's raw kept image, peer hunks included: {named}; "
            "write the recorded own-delta blob to the path and hand land again; nothing moved",
            "raw_kept_blob",
        )
    uncommitted = _uncommitted_only_imports(root, paths)
    if uncommitted is None:
        gate_unverified.append("uncommitted_import")
    elif uncommitted:
        return _refuse(
            "landing imports names that exist only uncommitted: " + "; ".join(uncommitted) + "; nothing moved",
            "uncommitted_import",
        )

    named_refusals = _uncommitted_attr_names(root, paths)
    if named_refusals is None:
        gate_unverified.append("landed_names")
    elif named_refusals:
        return _refuse(
            "landing names things that exist only uncommitted: " + "; ".join(named_refusals) + "; nothing moved",
            "uncommitted_name",
        )

    stranded = [
        {"path": h["path"], "kind": h["kind"], "reason": h["reason"], "run_id": h["run"]}
        for h in _stranded_siblings(root, paths)
    ]

    from coordinator_core.ops.dispatch_emit import stranded_sibling

    ownerless = stranded_sibling.ownerless(root, [p.replace("\\", "/") for p in paths])
    land_intent.record(root, params, kind="hand_land")
    land_intent.mark(land_intent.PHASE_COMMITTING, base=head)
    from coordinator_core.ops.ceremony import governed_by

    stamp = "\n".join([TRAILER, *governed_by.trailer_lines(root, params.get("run_id"))])
    try:
        for attempt in range(REF_CAS_ATTEMPTS):
            try:
                outcome = commit_paths(root, paths, f"{message.rstrip()}\n\n{stamp}\n")
                break
            except CommitRefused as exc:
                if not is_ref_moved(exc) or attempt == REF_CAS_ATTEMPTS - 1:
                    raise
    except (CommitRefused, OSError) as exc:
        land_intent.mark(land_intent.PHASE_CLAIMED)
        return _refuse(f"{type(exc).__name__}: {exc}", "commit")
    land_intent.mark(land_intent.PHASE_COMMITTED, outcome.sha)
    return {
        "ok": True,
        "sha": outcome.sha,
        "rebased": list(outcome.rebased),
        "sync_refused": list(outcome.sync_refused),
        **({"stranded": stranded} if stranded else {}),
        **({"ownerless": ownerless} if ownerless else {}),
        **({"gate_unverified": gate_unverified} if gate_unverified else {}),
        **sync_report(outcome),
    }


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    return hand_land(params or {}, repo_root)
