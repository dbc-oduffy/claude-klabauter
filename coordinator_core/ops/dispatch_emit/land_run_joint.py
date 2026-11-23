"""The joint mode of ``dispatch.land_run``: one landing of a co-tenant need cycle's halted runs.

``params["joint"]`` names the cycle's run ids. The call refuses unless ``co_tenant_cycle`` confirms the ids
form one cycle. The union is each run's halted-withheld-index entry (withheld plus declared paths) and the
dirty files under its record dir. A path a run withholds lands as HEAD plus each run's own delta
(``withheld-own-delta.json`` blob, 3-way applied in turn over its baseline), never a worktree or ``kept.json``
blob; an overlapping hunk refuses before any export. One ``clean_export`` over the union runs the test leg
over ``landing_reach.seeds_for_landing`` (a red whose failing ids all fail at HEAD is ``pre_existing``). The
union is then staged and committed once through ``combined_landing.land_combined`` (its ``_commit_index`` and
crediting), with a ``Joint-Land: <run_id>`` trailer per run; a run it leaves ``partial`` fails the landing.

Trap: the test leg exports the worktree bytes of every union path; a composed path is committed as HEAD plus
the runs' own deltas, so a foreign hunk riding in the worktree is tested but not committed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.git.run import GitResult
from coordinator_core.ops._path_guard import safe_id

JOINT_TRAILER = "Joint-Land"
MIN_RUNS = 2

REFUSED_IDS = "joint-ids"
REFUSED_NOT_CYCLE = "joint-not-a-cycle"
REFUSED_RUN = "joint-run"
REFUSED_COMPOSE = "joint-compose-unavailable"
REFUSED_OVERLAP = "joint-overlap"
REFUSED_LEG_RED = "joint-red"
REFUSED_LEG_INCONCLUSIVE = "joint-inconclusive"
REFUSED_EXPORT = "joint-export"
REFUSED_INDEX = "joint-index"
REFUSED_COMMIT = "joint-commit"

_ACCEPTED = ("green", "pre_existing")


def _refuse(error: str, refused: str, **extra: object) -> dict:
    return {"committed": False, "sha": None, "error": error, "refused": refused, **extra}


def _norm(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _git(root: Path, *args: str, stdin: Optional[bytes] = None) -> GitResult:
    from coordinator_core.git.run import run_git

    return run_git(["-C", str(root), *args], input=stdin)


def _ids(raw: object) -> Optional[list]:
    if not isinstance(raw, list) or any(not isinstance(i, str) or not safe_id(i) for i in raw):
        return None
    ids = sorted(dict.fromkeys(raw))
    return ids if len(ids) >= MIN_RUNS else None


def confirm_cycle(root: Path, ids: list) -> bool:
    """True when `ids` are one strongly connected set of need edges: from every id, following needs that stay
    inside `ids` reaches every other id. A set of three runs that all hold one path is one such set even
    though the shortest cycle `cycle_through` finds names two of them."""
    from coordinator_core.ops.dispatch_emit import co_tenant_cycle

    wanted = set(ids)

    def reach(start: str) -> set:
        seen, stack = {start}, [start]
        while stack:
            for nxt in co_tenant_cycle.needs(root, stack.pop()):
                if nxt in wanted and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    return all(reach(run_id) == wanted for run_id in ids)


def _own_deltas(root: Path, run_id: str) -> dict:
    """``{path: blob sha}`` of the run's ``withheld-own-delta.json``; empty when absent or unreadable."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE

    try:
        doc = json.loads((record_dir(root, run_id) / WITHHELD_OWN_DELTA_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    paths = doc.get("paths") if isinstance(doc, dict) else None
    out: dict = {}
    for path, spec in (paths.items() if isinstance(paths, dict) else ()):
        blob = spec.get("blob") if isinstance(spec, dict) else None
        if isinstance(path, str) and isinstance(blob, str) and blob:
            out[_norm(path)] = blob
    return out


def _blob(common: Path, sha: Optional[str]) -> Optional[bytes]:
    from coordinator_core.git.git_objects import read_object

    if sha is None:
        return b""
    got = read_object(common, sha)
    return got[1] if got is not None and got[0] == "blob" else None


def compose_path(root: Path, head, common: Path, path: str, runs: list) -> tuple:
    """``(bytes, None)`` of `path` as HEAD plus each of `runs` (``[(run_id, baseline sha | None, own-delta blob)]``)
    own delta applied in turn, or ``(None, reason)`` naming the run whose pre-image or hunk cannot apply."""
    from coordinator_core.git.own_delta_merge import apply_own_delta
    from coordinator_core.ops.dispatch_emit.grind_landing_select import _image_form

    head_raw = head.head_bytes(path) if head.in_head(path) else b""
    if head_raw is None:
        return None, f"HEAD bytes of {path} are unreadable"
    built = head_raw
    applied: list = []
    for run_id, pre_sha, blob_sha in runs:
        pre_raw, post_raw = _blob(common, pre_sha), _blob(common, blob_sha)
        if pre_raw is None or post_raw is None:
            return None, f"{path}: {run_id}'s baseline or own-delta blob is unreadable"
        pre, post, tip = (_image_form(root, path, raw, head_raw) for raw in (pre_raw, post_raw, built))
        if pre is None or post is None or tip is None:
            return None, f"{path}: {run_id}'s image has no line-ending-normal form"
        nxt = post if pre == tip else apply_own_delta(pre, post, tip)
        if nxt is None:
            return None, f"{path}: {run_id}'s hunk overlaps {', '.join(applied) if applied else 'a change already at HEAD'}"
        built = nxt
        applied.append(run_id)
    return built, None


def _plan(root: Path, ids: list) -> tuple:
    """``(plan, None)`` with ``plan = {union, composed: {path: bytes}}``, or ``(None, refusal reply)``."""
    from coordinator_core.authoring_leaks import HeadView
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution, halted_withheld_index

    entries = halted_withheld_index.entries(root)
    withheld: dict = {}
    union: set = set()
    for run_id in ids:
        entry = entries.get(run_id)
        if entry is None:
            return None, _refuse(f"{run_id} has no halted-withheld index entry", REFUSED_RUN, run_id=run_id)
        withheld[run_id] = {_norm(p) for p in entry.paths}
        union |= withheld[run_id] | {_norm(p) for p in entry.declared}
    head = HeadView(root, union)
    common = resolve_git_common_dir(root)
    composed: dict = {}
    deltas = {run_id: _own_deltas(root, run_id) for run_id in ids}
    baselines = {run_id: halted_hunk_attribution._baseline(root, run_id) or {} for run_id in ids}
    for path in sorted(set().union(*withheld.values())):
        runs: list = []
        for run_id in ids:
            if path not in withheld[run_id]:
                continue
            blob = deltas[run_id].get(path)
            baseline = baselines[run_id]
            if blob is None or path not in baseline:
                if sum(path in withheld[r] for r in ids) > 1:
                    return None, _refuse(
                        f"{path} is withheld by more than one run and {run_id} records no own delta and baseline for it; "
                        "a joint landing composes shared paths from own deltas only", REFUSED_COMPOSE, path=path, run_id=run_id,
                    )
                runs = []
                break
            runs.append((run_id, baseline[path], blob))
        if not runs:
            continue
        data, why = compose_path(root, head, common, path, runs)
        if data is None:
            return None, _refuse(
                f"{why} (path {path}, runs {', '.join(r[0] for r in runs)})",
                REFUSED_OVERLAP if "overlaps" in why else REFUSED_COMPOSE, path=path, runs=[r[0] for r in runs],
            )
        composed[path] = data
    return {"union": union, "composed": composed, "head": head}, None


def _record_dirty(root: Path, ids: list) -> list:
    """Dirty (modified, deleted or untracked) files under the runs' record dirs, one spawn."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    rels = [record_dir(root, run_id).relative_to(root).as_posix() for run_id in ids]
    done = _git(root, "ls-files", "-z", "-d", "-m", "-o", "--exclude-standard", "--", *rels)
    return [_norm(n) for n in done.stdout.split("\0") if n] if done.ok else []


def _judge(ran: subprocess.CompletedProcess) -> tuple:
    """``(verdict, failing ids, tail)`` of one pytest run: green, or red with the failing node ids."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    out = ran.stdout + ran.stderr
    tail = out.strip()[-2000:]
    if ran.returncode == 0 or cov.hygiene_only_exit(ran.returncode, out):
        return "green", (), tail
    if cov.collection_error(ran.returncode, out):
        return "red", cov.failing_ids(out), f"test collection failed: {tail}"
    return "red", cov.failing_ids(out), tail


def _pytest_in(root: Path, dest: Path, slug: str, seeds: list) -> subprocess.CompletedProcess:
    import shutil

    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
    from coordinator_core.testing import scoped_pytest

    holder, basetemp, tmp = scoped_pytest.per_run_dirs(root, slug)
    try:
        return cov._run_pytest_retrying(cov._pytest_argv(root, dest, basetemp, seeds), dest, cov._pytest_env(tmp, dest, seeds))
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def run_leg(root: Path, run_id: str, union: list, seeds: list) -> tuple:
    """``(verdict, detail)`` of one clean export over `union` and one pytest over `seeds`; a red whose failing
    ids all fail in a plain HEAD export is ``pre_existing``. Never raises: a setup failure is ``inconclusive``."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    argv = ["pytest", *seeds]
    try:
        with cov.clean_export(root, run_id, union, argv=argv) as dest:
            verdict, ids, tail = _judge(_pytest_in(root, dest, run_id, seeds))
        if verdict == "green":
            return "green", tail
        if ids:
            with cov.clean_export(root, f"{run_id}-base", (), argv=argv) as base:
                _, at_base, _ = _judge(_pytest_in(root, base, f"{run_id}-base", list(ids)))
            if set(ids) <= set(at_base):
                return "pre_existing", f"every failing test also fails at HEAD: {', '.join(ids)}"
        return "red", tail
    except cov.ExportError as exc:
        return "inconclusive", str(exc)
    except (OSError, subprocess.SubprocessError) as exc:
        return "inconclusive", f"joint test leg did not complete: {type(exc).__name__}: {exc}"


def _stage(root: Path, plan: dict, paths: list) -> Optional[str]:
    """Stage the union into the index: composed blobs by ``update-index --index-info``, the rest by ``add``.
    Returns an error text or None."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import write_object

    common = resolve_git_common_dir(root)
    head = plan["head"]
    lines = []
    for path, data in sorted(plan["composed"].items()):
        entry = head.entry(path)
        mode = f"{entry[0]:o}" if entry is not None else "100644"
        lines.append(f"{mode if mode.startswith(('100', '120')) else '100644'} {write_object(common, b'blob', data)}\t{path}")
    if lines:
        done = _git(root, "update-index", "--add", "--index-info", stdin=("\n".join(lines) + "\n").encode("utf-8"))
        if not done.ok:
            return f"git update-index failed: {done.stderr.strip() or done.returncode}"
    rest = [p for p in paths if p not in plan["composed"] and ((root / p).exists() or head.in_head(p))]
    if rest:
        done = _git(root, "--literal-pathspecs", "add", "-A", "--", *rest)
        if not done.ok:
            return f"git add failed: {done.stderr.strip() or done.returncode}"
    return None


def _message(ids: list) -> str:
    head = f"joint land: {' + '.join(ids)}"
    return head + "\n\n" + "\n".join(f"{JOINT_TRAILER}: {run_id}" for run_id in ids)


def land(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    """Mode entry ``land_run`` imports: land the cycle named by ``params["joint"]`` as one commit."""
    from coordinator_core.ops.dispatch_emit import combined_landing, co_tenant_cycle, landing_reach

    root = Path(worktree_root)
    ids = _ids(params.get("joint"))
    if ids is None:
        return _refuse(f"params.joint needs at least {MIN_RUNS} distinct safe run ids", REFUSED_IDS)
    if not confirm_cycle(root, ids):
        return _refuse(
            f"{', '.join(ids)} do not form one co-tenant need cycle; nothing landed",
            REFUSED_NOT_CYCLE, joint=ids, cause=co_tenant_cycle.CAUSE_CYCLE,
        )
    plan, refusal = _plan(root, ids)
    if refusal is not None:
        return refusal
    union = sorted(plan["union"] | set(_record_dirty(root, ids)))
    for run_id in ids:
        problem, _missing = combined_landing.classify(root, run_id, set(union))
        if problem:
            return _refuse(problem, REFUSED_RUN, run_id=run_id)
    seeds = landing_reach.seeds_for_landing(root, union)
    verdict, detail = run_leg(root, "+".join(ids), union, seeds) if seeds else ("green", "no test seeds in the union")
    if verdict not in _ACCEPTED:
        kind = REFUSED_LEG_RED if verdict == "red" else REFUSED_LEG_INCONCLUSIVE
        return _refuse(f"joint test leg {verdict}: {detail}", kind, verdict=verdict, seeds=seeds)

    staged = _git(root, "diff", "--cached", "--name-only", "-z")
    if not staged.ok:
        return _refuse(f"git diff --cached failed: {staged.stderr.strip() or staged.returncode}", REFUSED_INDEX)
    foreign = sorted({_norm(n) for n in staged.stdout.split("\0") if n} - set(union))
    if foreign:
        return _refuse(f"the index holds staged paths outside the union: {foreign[:5]}", REFUSED_INDEX)
    problem = _stage(root, plan, union)
    if problem:
        return _refuse(problem, REFUSED_INDEX)
    now = _git(root, "diff", "--cached", "--name-only", "-z")
    declared = sorted({_norm(n) for n in now.stdout.split("\0") if n}) if now.ok else []
    if not declared:
        return {"committed": False, "sha": None, "nothing_to_commit": True, "joint": ids}
    reply = combined_landing.land_combined(
        {"paths": declared, "run_ids": ids, "message": _message(ids)}, repo_root,
    )
    if not reply.get("ok"):
        _git(root, "reset", "-q", "--", *declared)
        return _refuse(str(reply.get("error")), REFUSED_COMMIT)
    out = {
        "committed": True, "sha": reply["sha"], "joint": ids, "landed": reply["landed"], "partial": reply["partial"],
        "paths": reply["committed"], "composed": sorted(plan["composed"]),
    }
    if verdict != "green":
        out["advisory"] = [f"joint test leg {verdict}: {detail}"]
    if reply["partial"]:
        out["error"] = "joint landing left a run partial: " + ", ".join(p["run_id"] for p in reply["partial"])
        out["incomplete_reasons"] = [f"{p['run_id']}: {', '.join(p['missing'][:5])}" for p in reply["partial"]]
    return out
