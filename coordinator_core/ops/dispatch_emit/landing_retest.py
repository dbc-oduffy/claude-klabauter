"""Retest-record reading for the landing gate: the run base, the fresh verdict the retest wrote, the
explicit ``failed_tests`` attribution and the unchanged-since-base proof. Imports only
``landing_targeted_tests`` at module level; ``run_close``, ``record_homes`` and git helpers load lazily."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit.landing_targeted_tests import (
    RETEST_LAUNCHER,
    named_failures,
)

RETEST_DIR = ".coordinator-local/retest"
RETEST_RUNNING = "running"
NO_TESTS_STATUS = "no-tests"
FAILED_TESTS_PARAM = "failed_tests"

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _submission_base(worktree_root: Path, run_id: str) -> Optional[str]:
    try:
        record = json.loads(
            (Path(worktree_root) / "scratch" / "warp" / run_id / "submission.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    value = record.get("run_base_sha") if isinstance(record, dict) else None
    return value if isinstance(value, str) and _SHA_RE.match(value) else None


def run_base_for(worktree_root: Path, run_id: Optional[str], params: Mapping) -> Optional[str]:
    """The run's base sha: ``params['run_base_sha']``, else the submission record's, else the
    reserved run's recorded base file; None when none exists."""
    value = params.get("run_base_sha")
    if isinstance(value, str) and _SHA_RE.match(value):
        return value
    if not run_id:
        return None
    found = _submission_base(worktree_root, run_id)
    if found:
        return found
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract

    return ask_fanout_contract.recorded_run_base(Path(worktree_root), run_id)


def fresh_verdict_path(worktree_root: Path, run_id: str) -> Path:
    """Where the retest step writes a run's fresh test verdict. Keyed on the bare run id: the writer
    receives ``<run>.workflow`` from a warp task output, the commit gate the bare ``<run>``."""
    from coordinator_core.ops.dispatch_emit.run_close import bare_run_id

    return Path(worktree_root) / RETEST_DIR / f"{bare_run_id(run_id)}.json"


def landing_fingerprint(worktree_root: Path, paths: Sequence[str]) -> dict[str, str]:
    """Repo-relative POSIX path to the sha256 hex of its bytes, or ``"absent"`` when unreadable.
    In-process; no spawn."""
    out: dict[str, str] = {}
    for rel in paths:
        key = str(rel).replace("\\", "/")
        try:
            out[key] = hashlib.sha256((Path(worktree_root) / key).read_bytes()).hexdigest()
        except OSError:
            out[key] = "absent"
    return out


def fresh_targeted_verdict(worktree_root: Path, run_id: str) -> Optional[Mapping]:
    """The retest verdict for ``run_id``, only while it still describes the tree: the record's
    ``run_id`` matches (``.workflow`` suffix ignored), no commit since the record's HEAD changed a ``covered`` path or a module in its import closure,
    and every ``covered`` file hashes as recorded. None otherwise, including for an unreadable or
    malformed file or an unreadable git tree.

    A ``stale`` record (``stale: true`` or status ``"stale"``) and a ``running`` one measured
    nothing, so neither is ever a fresh verdict."""
    from coordinator_core.git.git_state import head_sha

    try:
        record = json.loads(fresh_verdict_path(worktree_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    from coordinator_core.ops.dispatch_emit.run_close import bare_run_id

    recorded = record.get("run_id") if isinstance(record, dict) else None
    if not isinstance(recorded, str) or bare_run_id(recorded) != bare_run_id(run_id):
        return None
    if not isinstance(record.get("status"), str):
        return None
    if record.get("stale") is True or record["status"] in ("stale", RETEST_RUNNING):
        return None
    covered = record.get("covered")
    if not isinstance(covered, dict) or not isinstance(record.get("head"), str) or not record["head"]:
        return None
    head = head_sha(worktree_root)
    if not head:
        return None
    from coordinator_core.session.record_homes import home_dir

    # A run's own record dir (run-stage.json, halted.json, notes.jsonl, ...) is rewritten mid-call
    # by terminal_commit and is never its product. The trailing "/" keeps a sibling run whose id
    # shares this id's prefix judged.
    own_dir = (Path(home_dir(str(worktree_root), "warp")) / bare_run_id(run_id)).relative_to(
        Path(worktree_root)
    ).as_posix() + "/"
    judged = [p for p in covered if not str(p).replace("\\", "/").startswith(own_dir)]
    if record["head"] != head and not _footprint_unchanged_between(worktree_root, record["head"], head, judged):
        return None
    if landing_fingerprint(worktree_root, judged) != {p: covered[p] for p in judged}:
        return None
    return record


_CLOSURE_FILE_CAP = 400


def _module_files(root: Path, parts: Sequence[str]) -> list[str]:
    """Repo-relative POSIX files that module ``parts`` can resolve to (module file, then package ``__init__``)."""
    base = "/".join(parts)
    return [c for c in (f"{base}.py", f"{base}/__init__.py") if (root / c).is_file()]


def _imported_files(root: Path, rel: str) -> set[str]:
    """Repo files a Python file imports (every ``import`` in its AST, lazy ones included); in-process."""
    import ast

    try:
        tree = ast.parse((root / rel).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    pkg = rel.split("/")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                for depth in range(1, len(parts) + 1):
                    found.update(_module_files(root, parts[:depth]))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = pkg[: len(pkg) - (node.level - 1)] if node.level - 1 <= len(pkg) else []
            else:
                anchor = []
            mod = anchor + (node.module.split(".") if node.module else [])
            if node.level == 0 and not mod:
                continue
            found.update(_module_files(root, mod) if mod else [])
            for alias in node.names:
                found.update(_module_files(root, [*mod, alias.name]))
    return found


def _import_closure(root: Path, paths: Sequence[str]) -> list[str]:
    """``paths`` plus every repo ``.py`` file they import transitively, POSIX-normalised, capped."""
    seen: dict[str, None] = dict.fromkeys(str(p).replace("\\", "/") for p in paths)
    queue = [p for p in seen if p.endswith(".py")]
    while queue and len(seen) < _CLOSURE_FILE_CAP:
        for dep in sorted(_imported_files(root, queue.pop())):
            if dep not in seen:
                seen[dep] = None
                queue.append(dep)
    return list(seen)


def _footprint_unchanged_between(worktree_root: Path, old_sha: str, new_sha: str, covered: Sequence[str]) -> bool:
    """True only when no covered path, nor any module in their import closure, differs between the
    two commits' trees. An empty ``covered`` proves nothing, so it is False. One batched in-process tree read; any read failure is False (stale).
    The closure is read from the worktree: a module deleted by a peer commit, ``conftest.py`` and
    ``importlib`` imports are outside the footprint."""
    from coordinator_core.git import ref_cas_retry

    paths = _import_closure(Path(worktree_root), covered)
    if len(paths) >= _CLOSURE_FILE_CAP:
        return False
    if not paths:
        return False
    return ref_cas_retry.paths_unchanged_between(worktree_root, old_sha, new_sha, paths) is True


def _ran_nothing(tests_run: object) -> bool:
    """True when ``tests_run`` is absent or the integer 0 (anything else non-numeric reads as absent)."""
    if isinstance(tests_run, int) and not isinstance(tests_run, bool):
        return tests_run == 0
    return True


def _ran_no_tests(record: Mapping) -> bool:
    """True for a fresh ``no-tests`` record (every target dropped or none collected): it measured
    nothing and did not crash, so it is no verdict and the recorded verdict plus ``failed_tests``
    attribution decide. A launcher crash (``error`` without ``tests_run``) is not this."""
    return record.get("status") == NO_TESTS_STATUS


def _explicit_failed_tests(params: Mapping) -> list[str]:
    """Non-empty ``failed_tests`` node ids the caller passed (top level or ``params.params``): an
    attribution that outranks an errored no-results retest."""
    nested = params.get("params")
    out: list[str] = []
    for holder in (params, nested if isinstance(nested, Mapping) else {}):
        raw = holder.get(FAILED_TESTS_PARAM)
        out += [str(n) for n in raw] if isinstance(raw, (list, tuple)) else [raw] if isinstance(raw, str) else []
    return [n for n in out if n.strip()]


def _explicit_empty_failed_tests(params: Mapping) -> bool:
    """True when the caller passed ``failed_tests`` as an empty list (top level or ``params.params``)
    and no non-empty one."""
    nested = params.get("params")
    lists = [h.get(FAILED_TESTS_PARAM) for h in (params, nested if isinstance(nested, Mapping) else {})]
    lists = [x for x in lists if isinstance(x, (list, tuple))]
    return bool(lists) and not any(lists)


def _retest_record(worktree_root: Path, run_id: Optional[str]) -> Optional[dict]:
    """The run's retest record on disk, valid or not; None when absent, unreadable or not an object."""
    if not run_id:
        return None
    try:
        record = json.loads(fresh_verdict_path(worktree_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _retest_record_status(worktree_root: Path, run_id: Optional[str]) -> Optional[str]:
    """Status in the run's retest record on disk, valid or not; None when absent or unreadable."""
    record = _retest_record(worktree_root, run_id)
    status = record.get("status") if record else None
    return status if isinstance(status, str) else None


def _stale_retest_note(worktree_root: Path, run_id: Optional[str]) -> str:
    """Refusal suffix naming a retest record the tree has drifted from (HEAD moved or a covered
    file changed), with the route that clears it."""
    status = _retest_record_status(worktree_root, run_id)
    if status is None:
        return ""
    if status == "stale":
        return (
            f"; the run's retest record (stale) ran no tests: re-run {RETEST_LAUNCHER}, "
            f"or pass params.{FAILED_TESTS_PARAM}=[] after a passing retest"
        )
    return (
        f"; the run's retest record ({status}) no longer matches the tree (HEAD or a covered file "
        f"changed since it was written), so it is ignored: re-run {RETEST_LAUNCHER}, "
        f"or pass params.{FAILED_TESTS_PARAM}=[] after a passing retest"
    )


def _failed_nodes(params: Mapping, verdict: Optional[Mapping]) -> list[str]:
    """Failing node ids from every source the gate trusts, in order, deduplicated: the caller's
    ``failed_tests`` (top level, then ``params.params``), the verdict's, the digest's named ones."""
    nested = params.get("params")
    holders = (params, nested if isinstance(nested, Mapping) else {}, verdict if isinstance(verdict, Mapping) else {})
    out: list[str] = []
    for holder in holders:
        raw = holder.get(FAILED_TESTS_PARAM)
        out += [str(n) for n in raw] if isinstance(raw, (list, tuple)) else [raw] if isinstance(raw, str) else []
    out += named_failures(params.get("task_output_path"))
    return list(dict.fromkeys(n.strip() for n in out if n.strip()))


def _changed_since_base(worktree_root: Path, params: Mapping, run_id: Optional[str], files: list[str]) -> list[str]:
    """The ``files`` not proven unchanged since the run base (all of them when no base is known)."""
    base = run_base_for(worktree_root, run_id, params)
    if not base:
        return files
    from coordinator_core.ops.dispatch_emit import landing_integrity

    # Looked up on landing_integrity at call time, not bound from this module: tests patch the
    # landing_integrity attribute, and a module-level reference here would bypass the patch.
    unchanged = landing_integrity._unchanged_since_base
    if unchanged(worktree_root, base, files):
        return []
    if len(files) == 1:
        return files
    return [f for f in files if not unchanged(worktree_root, base, [f])]


def _unchanged_since_base(worktree_root: Path, base: str, paths: Sequence[str]) -> bool:
    """True when every path is byte-identical in the worktree, HEAD and ``base``; in-process."""
    from coordinator_core.git import ref_cas_retry
    from coordinator_core.git.git_state import head_blobs, head_sha

    head = head_sha(worktree_root)
    if not head or ref_cas_retry.paths_unchanged_between(worktree_root, base, head, paths) is not True:
        return False
    blobs = head_blobs(worktree_root, paths)
    for rel in paths:
        entry = blobs.get(rel)
        try:
            data = (Path(worktree_root) / rel).read_bytes()
        except OSError:
            return entry is None
        if entry is None or hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != entry[1]:
            return False
    return True
