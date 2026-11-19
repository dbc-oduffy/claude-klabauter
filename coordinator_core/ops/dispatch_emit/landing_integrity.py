"""Landing-integrity checks a run's terminal or halt commit makes before ``ceremony.commit_v2``.

Three named refusal reasons: a landed ``.py`` that fails to parse, a landing blob that drops
lines a foreign commit added since the run's base, and a targeted-test failure the run caused.
Everything is in-process: no spawn, and no test is run here, only the verdict the run carries
(or the fresh one ``retest_targeted`` wrote).

The verdict, dropped-target and retest-record defs live in ``landing_targeted_tests``,
``landing_dropped_targets`` and ``landing_retest``; this module re-imports them.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit import land_deadline

from coordinator_core.ops.dispatch_emit.landing_dropped_targets import (  # noqa: F401
    _blob_elsewhere,
    _classify_dropped,
    _dir_files,
    _index_or_empty,
    _only_run_deleted_targets,
    deleted_targets_receipt_note,
    renamed_dropped_targets,
    run_deleted_dropped_targets,
)
from coordinator_core.ops.dispatch_emit.landing_retest import (  # noqa: F401
    FAILED_TESTS_PARAM,
    NO_TESTS_STATUS,
    RETEST_DIR,
    RETEST_RUNNING,
    _SHA_RE,
    _explicit_empty_failed_tests,
    _explicit_failed_tests,
    _failed_nodes,
    _ran_no_tests,
    _ran_nothing,
    _retest_record,
    _retest_record_status,
    _stale_retest_note,
    _submission_base,
    fresh_targeted_verdict,
    fresh_verdict_path,
    landing_fingerprint,
    own_sizing_rel,
    run_base_for,
)
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import (  # noqa: F401
    NOT_RUN_CHECK,
    RETEST_ABANDONED,
    RETEST_LAUNCHER,
    REVERIFY_INDETERMINATE,
    REVERIFY_KEY,
    REVIEW_REVERIFY_FAILED,
    REVIEW_REVERIFY_INDETERMINATE,
    TARGETED_TESTS_FAILED,
    TARGETED_TESTS_PARAM,
    UNKNOWN_ONLY,
    UNRUN_OWED,
    ZERO_FAIL_CHECK,
    ZERO_FAIL_UNATTRIBUTED,
    ZERO_RUN_CAUSE_KEY,
    INVARIANT_UNMEASURED,
    Finding,
    tests_failed_finding,
    unmeasured_finding,
    zero_run_cause_note,
    _FILE_LIKE,
    _GATE_BLOCKER,
    _ZERO_RUN_CAUSES,
    _imports_landed,
    _indeterminate_finding,
    _module_files,
    _node_file,
    _repo_test_path,
    FAILING_AT_BASE,
    base_red_cleared,
    base_red_ids,
    baseline_caused_files,
    failed_targeted_tests,
    named_failures,
    norm_node_id,
    recorded_targeted_set,
    retest_remedy,
    reverify_failing_nodes,
    reverify_passed_clean,
    reverify_record,
    reverify_red_findings,
    reverify_verdict,
    sidecar_base_red,
    unevidenced_ids,
    targeted_set,
    targeted_test_verdict,
)

PY_UNPARSEABLE = "py-unparseable"
SINCE_BASE_OVERLAP = "since-base-overlap"
IMPORT_CLOSURE_UNTRACKED = "import-closure-untracked"
LOST_DEFINITION = "lost-definition"
UNREADABLE_PACK_INDEX = SINCE_BASE_OVERLAP

CLAIM_PROVENANCE_REASONS = (
    "import-closure-untracked", "peer-hunks-co-declared", "out-of-ask-hunks", "shipped-claim-unbacked",
)


def split_claim_provenance(findings: Sequence) -> tuple[list, list]:
    """``(refusing, provenance)``: findings of a claim kind leave the refusal list and are recorded,
    unless ``landing_refusal_kinds.is_refusal`` names the reason."""
    from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import is_refusal

    kept = [f for f in findings if f.reason not in CLAIM_PROVENANCE_REASONS or is_refusal(f.reason)]
    return kept, [f for f in findings if f not in kept]


_CLOSURE_MODULE = re.compile(r"\bfrom ([\w.]+);")


def provenance_entries(provenance: Sequence) -> list[dict]:
    """The reply shape of recorded provenance findings. An import-closure entry also carries
    ``importer`` (the landing path) and ``module`` (the dotted module the detector named, "" if none); consumers are the
    ``provenance``/``advisory`` reply fields of ``land_run`` and ``land_run_halt``."""
    out: list[dict] = []
    for f in provenance:
        entry = {"path": f.path, "reason": f.reason, "detail": f.detail}
        if f.reason == IMPORT_CLOSURE_UNTRACKED:
            found = _CLOSURE_MODULE.search(f.detail or "")
            entry["importer"] = f.path
            entry["module"] = found.group(1) if found else ""
        out.append(entry)
    return out


def unparseable_py(worktree_root: Path, paths: Sequence[str]) -> list[Finding]:
    """A Finding(PY_UNPARSEABLE) per existing ``.py`` path whose worktree bytes fail ``ast.parse``.
    Non-``.py`` and missing paths are skipped."""
    items: list[tuple[str, bytes]] = []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        land_deadline.check("landing_integrity.unparseable_py")
        try:
            items.append((rel, (Path(worktree_root) / rel).read_bytes()))
        except OSError:
            continue
    return [f for f in _cached_parse_findings(worktree_root, items) if f]


_VERDICT_DIR = "coordinator-parse-verdicts"
_VERDICT_KEEP = 256
_VERDICT_MAX = 512


def _git_common_dir(worktree_root: Path) -> Optional[Path]:
    """The repo's common git dir read from ``.git`` on disk (no spawn); None when not a checkout."""
    try:
        dot = Path(worktree_root) / ".git"
        if dot.is_file():
            text = dot.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text[7:].strip())
            if not gitdir.is_absolute():
                gitdir = dot.parent / gitdir
            common = gitdir / "commondir"
            if common.is_file():
                gitdir = gitdir / common.read_text(encoding="utf-8").strip()
            return gitdir
        return dot if dot.is_dir() else None
    except OSError:
        return None


def _verdict_path(worktree_root: Path, source: bytes, common: Optional[Path] = None) -> Optional[Path]:
    """Verdict file for ``(sha256(source), python major.minor)``: the grammar is part of the verdict.
    ``common`` is the already-resolved git common dir (resolved here when omitted)."""
    import hashlib
    import sys

    if common is None:
        common = _git_common_dir(worktree_root)
    if common is None:
        return None
    key = f"{hashlib.sha256(source).hexdigest()}-py{sys.version_info[0]}{sys.version_info[1]}"
    return common / _VERDICT_DIR / key


def _read_verdict(path: Path) -> Optional[tuple]:
    """``("ok", "")`` / ``("bad", detail)`` from a verdict file; None (a miss) when absent or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("ok") is True:
        return ("ok", "")
    if isinstance(data, dict) and data.get("ok") is False and isinstance(data.get("detail"), str):
        return ("bad", data["detail"])
    return None


_WRITE_QUEUE_MAX = 256
_write_queue = None
_force_async = False


def _write_verdict(path: Path, found: Optional[Finding]) -> None:
    """Hand the record to one per-process writer thread; never blocks the caller, never raises.

    A file create under the git common dir can block in the kernel for minutes (a storage-optimizer
    pass on the drive); the cache is best-effort, so a stalled create delays only the writer and a
    full queue drops the record (the next reader re-parses).
    """
    import queue
    import sys
    import threading

    global _write_queue
    if "pytest" in sys.modules and not _force_async:
        _store_verdict(path, found)
        return
    try:
        if _write_queue is None:
            _write_queue = queue.Queue(maxsize=_WRITE_QUEUE_MAX)

            def _drain(q) -> None:
                while True:
                    item = q.get()
                    _store_verdict(*item)

            threading.Thread(target=_drain, args=(_write_queue,), name="parse-verdict-writer", daemon=True).start()
        _write_queue.put_nowait((path, found))
    except Exception:
        pass


def _store_verdict(path: Path, found: Optional[Finding]) -> None:
    """Atomic best-effort record; evicts the oldest entries past ``_VERDICT_MAX``. Never raises."""
    import os

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"ok": True} if found is None else {"ok": False, "detail": found.detail}
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        names = os.listdir(path.parent)
        if len(names) > _VERDICT_MAX:
            aged = []
            for n in names:
                try:
                    aged.append(((path.parent / n).stat().st_mtime, n))
                except OSError:
                    continue
            aged.sort()
            for _, n in aged[: len(aged) - _VERDICT_KEEP]:
                try:
                    (path.parent / n).unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _cached_parse_finding(worktree_root: Path, rel: str, source: bytes) -> Optional[Finding]:
    """``_parse_finding`` memoised on disk by sha256(bytes)+interpreter version under the git common dir,
    so a later process parses nothing a producer already parsed. A missing or malformed entry is a miss."""
    return _cached_parse_findings(worktree_root, [(rel, source)])[0]


def _cached_parse_findings(worktree_root: Path, items: Sequence[tuple[str, bytes]]) -> list[Optional[Finding]]:
    """``_cached_parse_finding`` over ``(rel, source)`` pairs, one finding (or None) per pair in order.
    The common dir is resolved once; each distinct verdict is read once, and a miss is parsed and
    recorded through the never-blocking ``_write_verdict``."""
    common = _git_common_dir(worktree_root) if items else None
    out: list[Optional[Finding]] = []
    seen: dict[Path, Optional[tuple]] = {}
    for rel, source in items:
        path = _verdict_path(worktree_root, source, common) if common is not None else None
        if path is None:
            out.append(_parse_finding(rel, source))
            continue
        if path in seen:
            hit = seen[path]
        else:
            hit = seen[path] = _read_verdict(path)
        if hit is not None:
            out.append(None if hit[0] == "ok" else Finding(rel, PY_UNPARSEABLE, hit[1]))
            continue
        found = _parse_finding(rel, source)
        _write_verdict(path, found)
        seen[path] = ("ok", "") if found is None else ("bad", found.detail)
        out.append(found)
    return out


def _parse_finding(rel: str, source: bytes) -> Optional[Finding]:
    try:
        ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return Finding(rel, PY_UNPARSEABLE, f"line {exc.lineno}: {exc.msg}")
    except ValueError as exc:
        return Finding(rel, PY_UNPARSEABLE, f"line 0: {exc}")
    return None


def unparseable_py_at(worktree_root: Path, rev: str, paths: Sequence[str]) -> list[Finding]:
    """Like ``unparseable_py`` but parses each ``.py`` path's blob at ``rev`` (one ``git cat-file --batch``
    spawn). Paths absent at ``rev`` are skipped; an unreadable ``rev`` yields no findings."""
    wanted = [p for p in paths if p.endswith(".py")]
    if not wanted:
        return []
    from coordinator_core.git.run import run_git

    proc = run_git(
        ["-C", str(worktree_root), "cat-file", "--batch"],
        input="".join(f"{rev}:{p}\n" for p in wanted).encode("utf-8"),
        binary=True,
    )
    if proc.returncode != 0:
        return []
    data, pos, items = proc.stdout_bytes, 0, []
    for rel in wanted:
        eol = data.find(b"\n", pos)
        if eol < 0:
            break
        header = data[pos:eol].split()
        pos = eol + 1
        if len(header) != 3 or header[1] != b"blob":
            continue
        size = int(header[2])
        items.append((rel, data[pos : pos + size]))
        pos += size + 1
    return [f for f in _cached_parse_findings(worktree_root, items) if f]


def closing_bases(
    worktree_root: Path, run_id: Optional[str], params: Mapping, landed_paths: Optional[Iterable[str]] = None,
    plan_wide: bool = False,
) -> tuple:
    """The 40-hex bases a run's closing or halt trailer names, deduplicated, in order: the stage
    manifest ``head_sha``, then ``run_base_for``, then ``params["review_run_base"]``.

    ``landed_paths`` (``None`` = off; any iterable, even empty, = on) appends the base of every other
    open run-open checkpoint of the run's plan (matched by plan file name, so a moved plan still
    matches) whose ``Checkpoint-Withheld`` paths are all in ``landed_paths``
    (``_open_sibling_bases``; one git spawn). ``plan_wide`` turns the sibling scan on with no coverage
    gate: every open run-open checkpoint of the run's plan, whatever its withheld paths (a supersession
    close, which has no landing to cover them).

    The manifest comes first because the ask route's checkpoints carry it as ``Checkpoint-Base``
    (``emit._checkpoint_trailer_js`` stamps ``_RUN_BASE_VAR`` from the emit sha), and it differs
    from the submission ``run_base_sha``. A trailing ``.workflow`` is stripped from ``run_id``.
    A run whose ledger names ``resumed_from`` also closes each predecessor's bases (manifest and
    recorded base), appended after its own, following the chain cycle-safe for at most
    ``requirements_ledger.CARRIED_HOP_CAP`` hops.
    In-process file reads only; empty when nothing resolves."""
    rid = run_id[: -len(".workflow")] if isinstance(run_id, str) and run_id.endswith(".workflow") else run_id
    found: list = [*_own_bases(worktree_root, rid, params), params.get("review_run_base")]
    for prev in _resumed_chain(worktree_root, rid):
        found.extend(_own_bases(worktree_root, prev, {}))
    own = tuple(dict.fromkeys(b for b in found if isinstance(b, str) and re.fullmatch(r"[0-9a-f]{40}", b)))
    if landed_paths is None and not plan_wide:
        return own
    siblings = _open_sibling_bases(worktree_root, rid, params, own, landed_paths or (), plan_wide)
    return tuple(dict.fromkeys((*own, *siblings)))


_SIBLING_LOG_CAP = 400
_CAP_WARNED: set = set()


def _run_plan_names(worktree_root: Path, rid: Optional[str], params: Mapping) -> frozenset:
    """File names of the plans the run names: ``params`` ``plan_path``/``plan_rel`` and each ``resume.json`` ``resume_plan_rel`` along the resume chain."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    rels: list = [params.get("plan_path"), params.get("plan_rel")]
    for one in ([rid, *_resumed_chain(worktree_root, rid)] if rid else []):
        try:
            data = json.loads((record_dir(Path(worktree_root), one) / "resume.json").read_text(encoding="utf-8"))
            rels.append(data.get("resume_plan_rel") if isinstance(data, dict) else None)
        except (OSError, ValueError):
            continue
    return frozenset(r.replace("\\", "/").rsplit("/", 1)[-1] for r in rels if isinstance(r, str) and r.strip())


def _open_sibling_bases(
    worktree_root: Path, rid: Optional[str], params: Mapping, known: Sequence, landed_paths: Iterable[str],
    plan_wide: bool = False, uncapped: bool = False, since: Optional[str] = None,
) -> list:
    """Bases of open run-open checkpoints, not in ``known``, of the run's plan whose withheld paths are all covered.

    ``since`` (a rev) bounds the log to ``<since>^@..HEAD`` in place of ``known``'s oldest base.
    ``uncapped`` lifts the commit cap of an unbounded scan, narrowing the log to ``Checkpoint-``
    trailer commits instead.

    Covered: every withheld path is named in ``landed_paths`` (the run's own landing; a later commit
    by another run never counts). Open: no ``Checkpoint-Run-Closed`` in the log names the base.
    ``known`` is the run's bases, resumed-chain predecessors last. One ``git log`` over the run's base
    range (``<oldest known base>^@..HEAD``), so depth never hides a checkpoint; with no base the log is
    capped at ``_SIBLING_LOG_CAP`` commits. What the scan could not reach is logged as a warning: known
    bases the range did not contain, or the commit the capped scan stopped at. A checkpoint with no
    withheld paths never closes here; empty when the log fails."""
    plans = _run_plan_names(worktree_root, rid, params)
    if not plans:
        return []
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
        _forward,
        checkpoint_withheld,
        parse_checkpoint,
        run_closed_bases,
    )

    base_args = ["-c", "core.quotepath=off", "log", "--format=%x1e%H%x1d%B%x1f"]
    bounds = [since] if since else list(known)
    ranged = bool(bounds)
    if ranged:
        done = run_git([*base_args, "HEAD", "--not", f"{bounds[-1]}^@"], cwd=str(worktree_root))
        if done.returncode != 0:
            ranged = False
    if not ranged:
        tail = ["-E", "--grep=^Checkpoint-"] if uncapped else [f"-n{_SIBLING_LOG_CAP}"]
        done = run_git([*base_args, *tail], cwd=str(worktree_root))
    if done.returncode != 0:
        return []
    covered = {_forward(p).casefold() for p in landed_paths if isinstance(p, str)}
    closed: set = set()
    candidates: list = []
    seen: set = set()
    last = ""
    for record in done.stdout.split("\x1e"):
        head, sep, _ = record.partition("\x1f")
        last, _, message = head.partition("\x1d")
        if not sep:
            continue
        seen.add(last)
        closed |= run_closed_bases(message)
        cp = parse_checkpoint(message)
        if cp is not None and cp.not_landed and cp.base and cp.plan_rel.rsplit("/", 1)[-1] in plans:
            need = {_forward(p).casefold() for p in checkpoint_withheld(message)}
            if plan_wide or (need and need <= covered):
                candidates.append(cp.base)
    log = logging.getLogger(__name__)
    if ranged:
        missed = [b for b in bounds if b not in seen]
        if missed:
            log.warning(
                "sibling checkpoint scan range did not contain base(s) %s; run-open checkpoints older than them were not scanned",
                ", ".join(m[:12] for m in missed),
            )
    elif not uncapped and len(seen) >= _SIBLING_LOG_CAP and _SIBLING_LOG_CAP not in _CAP_WARNED:
        # No base to bound by: any repo deeper than the cap hits it on every call; once per process per cap keeps it a signal.
        _CAP_WARNED.add(_SIBLING_LOG_CAP)
        log.warning(
            "sibling checkpoint scan hit the %d-commit cap with no run base to bound it; checkpoints older than %s were not scanned",
            _SIBLING_LOG_CAP, last[:12],
        )
    skip = set(known)
    return [b for b in candidates if b not in skip and b not in closed]


def open_plan_bases(worktree_root: Path, plan_path: str, since: Optional[str] = None) -> list:
    """Bases of every open run-open checkpoint whose own ``Checkpoint-Plan`` file name is ``plan_path``'s, newest first; no run needed.

    One ``git log`` over the ``Checkpoint-`` trailer commits (``since``: only ``<since>^@..HEAD``), never capped."""
    return _open_sibling_bases(worktree_root, None, {"plan_path": plan_path}, (), (), True, uncapped=True, since=since)


def _own_bases(worktree_root: Path, rid: Optional[str], params: Mapping) -> list:
    """One run's unfiltered candidate bases: ledger manifest ``head_sha`` then its recorded run base."""
    found: list = []
    if rid:
        try:
            from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

            data = json.loads((record_dir(Path(worktree_root), rid) / "manifest.json").read_text(encoding="utf-8"))
            found.append(data.get("head_sha") if isinstance(data, dict) else None)
        except (OSError, ValueError):
            pass
    found.append(run_base_for(worktree_root, rid, params))
    return found


def _resumed_chain(worktree_root: Path, rid: Optional[str]) -> list:
    """The ``resumed_from`` predecessors of ``rid``, nearest first; empty without a ledger link."""
    if not rid:
        return []
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    out: list = []
    visited = {rid}
    cur = rid
    for _ in range(rl.CARRIED_HOP_CAP):
        try:
            prev = (rl.read_run(rl.run_dir(Path(worktree_root), cur)) or {}).get(rl.RESUMED_FROM_FIELD)
        except rl.LedgerRefused:
            break
        if not (isinstance(prev, str) and safe_id(prev)) or prev in visited:
            break
        visited.add(prev)
        out.append(prev)
        cur = prev
    return out


class GateFindings(list):
    """The gate's findings list; ``deleted_note`` is the receipt line naming run-deleted targets
    (``""`` when none). ``unknown_ids`` are the failing ids with no baseline evidence (check
    ``"unknown-only"``): never refused on; no production caller reads them yet (tests only), so they are
    speculative until a receipt surfaces them. A plain list otherwise, so ``== []`` and unpacking keep working."""

    deleted_note: str = ""
    foreign_evidence: Optional[dict] = None
    unrun_owed: Sequence[str] = ()
    unknown_ids: Sequence[str] = ()
    peer_advisory: Sequence[Finding] = ()


class RetestAwait(Finding):
    """The gate's answer while the run's retest record is ``running``: not a verdict, an instruction to
    poll. Carries reason ``TARGETED_TESTS_FAILED`` so consumers that treat that reason as advisory
    are unchanged; ``retest_await`` is the one reader that tells it apart."""


def retest_await(findings: Sequence[Finding], params: Mapping) -> Optional[dict]:
    """``{awaiting, poll, rerun}`` when ``findings`` is the gate's running-retest answer, else None.
    ``poll`` reads the record to a verdict; ``rerun`` restarts it, owed only once the poll reports
    the retest abandoned past its deadline. A landing that reads it re-invokes after the poll and
    never blocks in the engine."""
    if not any(isinstance(f, RetestAwait) for f in findings):
        return None
    return {
        "awaiting": "retest",
        "poll": retest_remedy(params) + " --poll",
        "rerun": retest_remedy(params),
    }


def landing_test_gate(
    worktree_root: Path, params: Mapping, run_id: Optional[str],
    landed_paths: Optional[Sequence[str]] = None, collect_unrun: bool = False,
) -> tuple[GateFindings, str]:
    """``_landing_test_gate_core`` plus the invariant gate: with ``landed_paths`` given, every
    repo-wide invariant whose measured set they touch must be in the run's fresh passing retest,
    else one ``TARGETED_TESTS_FAILED`` finding per missing invariant (check ``"failed"``).
    ``collect_unrun`` (the landing entry, which can launch the retest) returns the owed nodes nothing
    measured in ``GateFindings.unrun_owed`` instead of findings; ``run_unrun_owed`` settles them."""
    findings, check = _landing_test_gate_core(worktree_root, params, run_id)
    if check == UNRUN_OWED and not collect_unrun:
        findings.extend(unrun_finding(n, params) for n in findings.unrun_owed)
        findings.unrun_owed, check = (), "failed"
    if not landed_paths or check == "retest-running":
        return findings, check
    foreign: dict = {}
    unrun: Optional[list] = [] if collect_unrun else None
    owed = invariant_findings(worktree_root, landed_paths, run_id, params, foreign, unrun)
    carried = findings.foreign_evidence or {}
    unrun = list(dict.fromkeys([*findings.unrun_owed, *(unrun or ())]))
    if not owed:
        if foreign:
            findings.foreign_evidence = {**carried, **foreign}
        findings.unrun_owed = unrun
        return findings, check
    out = GateFindings([*findings, *owed])
    out.deleted_note = findings.deleted_note
    out.foreign_evidence = {**carried, **foreign}
    out.unrun_owed, out.unknown_ids = unrun, findings.unknown_ids
    out.peer_advisory = findings.peer_advisory
    return out, "failed"


def unrun_finding(node: str, params: Mapping) -> Finding:
    """The refusal for an owed invariant ``node`` nothing measured on this tree."""
    return tests_failed_finding(
        node.split("::")[0],
        f"repo-wide invariant {node} measures a path this landing touches and no fresh passing "
        f"retest ran it; re-run: {retest_remedy(params, [node])}",
    )


def _retest_input(worktree_root: Path, params: Mapping, run_id: Optional[str]) -> Optional[tuple]:
    """``(flag, value)`` the detached retest reads its params from: the run's params file when written,
    else the task-output file; None when neither exists."""
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path

    canonical = params_path(worktree_root, run_id)
    if canonical is not None and canonical.is_file():
        return "--params-file", str(canonical)
    raw = params.get("task_output_path")
    if isinstance(raw, str) and raw.strip() and Path(raw).is_file():
        return "--from-task-output", raw
    return None


def run_unrun_owed(
    worktree_root: Path, params: Mapping, run_id: Optional[str], tested: GateFindings,
) -> GateFindings:
    """Settle ``tested.unrun_owed``: when no other test-evidence finding stands, launch ``retest_targeted``'s
    detached entry for exactly those nodes and answer one ``RetestAwait`` per node (poll, then re-invoke at
    the same arguments); otherwise, or when no params source exists or the child fails to spawn, the
    nodes become the ``unrun_finding`` refusals. The one process created is the detached child; the op
    never waits on it. Mutates and returns ``tested``."""
    nodes = list(tested.unrun_owed)
    tested.unrun_owed = ()
    if not nodes:
        return tested
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import TEST_EVIDENCE_REASONS

    source = _retest_input(worktree_root, params, run_id) if run_id else None
    if source is None or any(f.reason in TEST_EVIDENCE_REASONS for f in tested) or not _launch_retest(
        worktree_root, run_id, source, nodes,
    ):
        tested.extend(unrun_finding(n, params) for n in nodes)
        return tested
    poll = retest_remedy(params) + " --poll"
    tested.extend(
        RetestAwait(
            "", TARGETED_TESTS_FAILED,
            f"repo-wide invariant {n} had no fresh run; its retest was launched; poll: {poll}, then re-invoke "
            "this landing with the same arguments",
        )
        for n in nodes
    )
    return tested


def _launch_retest(worktree_root: Path, run_id: Optional[str], source: tuple, nodes: Sequence[str]) -> bool:
    """Start ``retest_targeted`` detached on ``nodes`` (isolated export); True when its record reads running."""
    import argparse
    import contextlib
    import io

    from coordinator_core.ops.dispatch_emit import retest_targeted as rt

    flag, value = source
    args = argparse.Namespace(
        params_file=value if flag == "--params-file" else None,
        from_task_output=value if flag == "--from-task-output" else None, isolated_export=True,
    )
    path = fresh_verdict_path(worktree_root, run_id)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = rt._start_detached(
            args, Path(worktree_root), path, path.with_suffix(".log"), str(run_id), list(nodes), rt.spawn_detached,
        )
    return code == 0


def run_landing_paths(
    worktree_root: Path, all_paths: Sequence[str], deleted_paths: Sequence[str] = (),
    record_abs: Optional[Path] = None,
) -> list[str]:
    """The product landing set the integrity gate judges: ``all_paths`` less the deleted paths and
    the run's own record file."""
    skip = set(deleted_paths)
    if record_abs is not None:
        try:
            skip.add(Path(record_abs).resolve().relative_to(Path(worktree_root).resolve()).as_posix())
        except ValueError:
            pass
    return [p for p in all_paths if p not in skip]


def _own_artifacts(worktree_root: Path, run_id: str) -> tuple[str, frozenset]:
    """``(own_dir_prefix, own_files)``: the run's record dir (trailing ``/``) and its sizing and plan
    paths. Engine-written run artifacts, never a chunk's product; they select no invariant."""
    from coordinator_core.ops.dispatch_emit.run_close import bare_run_id
    from coordinator_core.ops.dispatch_emit.run_write_set import _plan_rel
    from coordinator_core.session.record_homes import home_dir

    root = Path(worktree_root)
    base = Path(home_dir(str(root), "warp")) / bare_run_id(run_id)
    own_dir = base.relative_to(root).as_posix() + "/"
    sizing = own_sizing_rel(root, base)
    plan = _plan_rel(root, {}, sizing or None)
    files = {s.replace("\\", "/") for s in (sizing, plan) if s}
    return own_dir, frozenset(files)


def landing_invariant_nodes(
    worktree_root: Path, landed_paths: Sequence[str], run_id: Optional[str],
) -> list[str]:
    """The existing repo-wide invariant node ids a landing set selects, less paths other halted
    runs withhold (residue at HEAD, not this run's diff). The set ``invariant_findings`` owes."""
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    if run_id:
        from coordinator_core.ops.dispatch_emit.halted_hold_release import residue_paths

        residue = residue_paths(worktree_root, run_id, paths=landed_paths)
        if residue:
            landed_paths = [p for p in landed_paths if p.replace("\\", "/") not in residue]
        own_dir, own_files = _own_artifacts(worktree_root, run_id)
        landed_paths = [
            p for p in landed_paths
            if not (norm := p.replace("\\", "/")).startswith(own_dir) and norm not in own_files
        ]
    nodes = list(imp.existing_invariants(worktree_root, imp.invariants_for_paths(landed_paths, worktree_root)))
    if not landed_paths:
        return nodes
    from coordinator_core.ops.dispatch_emit import always_run_guards

    return nodes + [g for g in always_run_guards.declared_guards(worktree_root) if g not in nodes]


def _red_nodes(fresh: Optional[Mapping], foreign: set[str]) -> tuple[set[str], bool]:
    """``(red, only_base_red)``: the node ids a fresh ``fail`` verdict's baseline names failing, and
    whether every one is evidenced red at the run base with nothing caused. Empty/False off a fail
    verdict with a baseline."""
    if not (fresh and fresh.get("status") == "fail" and isinstance(fresh.get("baseline"), Mapping)):
        return set(), False
    base = fresh["baseline"]
    red = {
        str(i.get("id") if isinstance(i, Mapping) else i)
        for key in ("caused", "pre_existing", "foreign", "unverified") for i in (base.get(key) or [])
    }
    return red, bool(red) and not base.get("caused") and red <= foreign


def _guard_hit_verdict(
    node: str, fresh: Mapping, landed_paths: Sequence[str], params: Mapping,
) -> Optional[Finding]:
    """The finding for an always-run guard ``node`` red in the fresh retest, or None when the red is
    baseline-only. A guard red at HEAD is judged by touched-file hits (tree above HEAD), never by its
    verdict. No measured hits for the node: unmeasured, never credited."""
    from coordinator_core.ops.dispatch_emit import always_run_guards as arg
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import retest_remedy

    entry = (fresh.get(arg.GUARD_HITS_KEY) or {}).get(node)
    if not isinstance(entry, Mapping):
        return unmeasured_finding(
            node.split("::")[0],
            f"always-run guard {node} is red and its touched-file hits were not measured against HEAD; "
            f"re-run: {retest_remedy(params, [node])}",
        )
    touched = {p.replace("\\", "/") for p in landed_paths}
    new = [p for p in arg.new_hits(entry.get("head") or {}, entry.get("tree") or {}) if p in touched]
    if not new:
        return None
    return tests_failed_finding(
        node.split("::")[0], f"always-run guard {node} has new hits in touched files: {', '.join(sorted(new))}",
    )


def invariant_findings(
    worktree_root: Path, landed_paths: Sequence[str], run_id: Optional[str], params: Mapping,
    foreign_out: Optional[dict] = None, unrun_out: Optional[list] = None,
) -> list[Finding]:
    """One finding per repo-wide invariant a landing diff touches that no fresh passing retest
    ran. An invariant node red in a fresh ``fail`` retest and in its ``baseline.evidence`` (red on a
    pure-HEAD export), or named red at the run base by the run's own records, is credited foreign, not
    a finding; ``foreign_out`` receives ``{node: evidence}``. With ``unrun_out`` given and the retest
    budget unspent, a node nothing measured is appended there instead of becoming a finding.
    Table lookup plus one record read; zero spawns. With the in-run retest budget spent and no verdict,
    nothing measured the node: it is an ``INVARIANT_UNMEASURED`` finding naming the retest remedy, never
    ``TARGETED_TESTS_FAILED`` (reserved for a measured failure). Paths other halted runs withhold
    (``halted_hold_release.residue_paths``) are residue at HEAD, not this run's diff: they select no
    invariant. Residue bytes are never touched."""
    from coordinator_core.ops.dispatch_emit import always_run_guards as _arg

    config = [unmeasured_finding("coordinator.local.md", p) for p in _arg.config_problems(worktree_root)]
    owed = landing_invariant_nodes(worktree_root, landed_paths, run_id)
    if not owed:
        return config
    fresh = fresh_targeted_verdict(worktree_root, run_id) if run_id else None
    ran = set(fresh.get("tests") or []) if fresh and fresh.get("status") == "pass" else set()
    foreign: set[str] = set()
    if fresh and fresh.get("status") == "fail" and isinstance(fresh.get("baseline"), Mapping):
        from coordinator_core.completion_receipts.verdict import evidenced_clear_ids

        # Trap: trusted only because fresh_targeted_verdict returned an engine-written record that
        # matches this tree's fingerprint; a carried record must pass measured=False.
        foreign = evidenced_clear_ids(fresh["baseline"], measured=True)
    red, only_base_red = _red_nodes(fresh, foreign)
    guards = set(_arg.declared_guards(worktree_root))
    base_red = {
        norm_node_id(n) for n in (base_red_ids(params) | sidecar_base_red(worktree_root, params))
    } if params else set()
    out = list(config)
    for node in owed:
        if node in ran or node.split("::")[0] in ran:
            continue
        if node in guards and any(r == node or r.startswith((node + "::", node + "[")) for r in red):
            verdict = _guard_hit_verdict(node, fresh, landed_paths, params)
            if verdict is not None:
                out.append(verdict)
            elif foreign_out is not None:
                foreign_out[node] = [node] if node in (fresh["baseline"].get("evidence") or []) else []
            continue
        if only_base_red and node not in red:
            continue
        if node in foreign:
            if foreign_out is not None:
                foreign_out[node] = [e for e in (fresh["baseline"].get("evidence") or []) if e == node]
            continue
        from coordinator_core.ops.dispatch_emit.landing_targeted_tests import base_check_red, retests_spent

        if norm_node_id(node) in base_red or (fresh is not None and base_check_red(fresh, [node])[0]):
            if foreign_out is not None:
                foreign_out[node] = [FAILING_AT_BASE]
            continue
        actual = sorted(red - {node}) if node not in red else []
        spent = retests_spent(params)
        if spent and actual:
            detail = (
                f"repo-wide invariant {node} did not fail; the in-run retest failed on "
                f"{', '.join(actual[:3])}"
            )
        elif spent and fresh is None:
            detail = (
                f"repo-wide invariant {node}: no fresh retest verdict exists, so nothing measured it; "
                f"re-run: {retest_remedy(params, [node])}"
            )
        elif spent and fresh.get("status") == "pass":
            detail = f"repo-wide invariant {node}: the in-run retest passed without running it"
        elif spent and fresh.get("status") != "fail":
            detail = (
                f"repo-wide invariant {node}: the in-run retest ended "
                f"{fresh.get('status') or 'without a status'}, measuring no failure"
            )
        elif spent:
            detail = f"repo-wide invariant {node} failed its in-run retest"
        else:
            if node not in red and unrun_out is not None:
                unrun_out.append(node)
            else:
                out.append(unrun_finding(node, params))
            continue
        make = unmeasured_finding if spent and fresh is None and not actual else tests_failed_finding
        out.append(make(node.split("::")[0], detail))
    return out


def _landing_test_gate_core(
    worktree_root: Path, params: Mapping, run_id: Optional[str]
) -> tuple[GateFindings, str]:
    """``(findings, check)`` as ``_landing_test_gate``; on a benign ``no-verdict`` or ``not-run``
    result, ``findings.deleted_note`` names the dropped targets the run's own diff deleted, so every
    caller reads one computation. ``check`` is never changed by the note."""
    findings, check = _landing_test_gate(worktree_root, params, run_id)
    out = findings if isinstance(findings, GateFindings) else GateFindings(findings)
    if not findings and check in ("no-verdict", NOT_RUN_CHECK):
        out.deleted_note = "; ".join(
            n for n in (out.deleted_note, deleted_targets_receipt_note(worktree_root, params, run_id)) if n
        )
    return out, check


def _measured_record(worktree_root: Path, run_id: Optional[str]) -> Optional[dict]:
    """The run's ``measured-rev.json`` record, or None when absent, unreadable or not an object."""
    if not run_id:
        return None
    from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

    try:
        data = json.loads(measured_rev_record_path(worktree_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _landing_test_gate(
    worktree_root: Path, params: Mapping, run_id: Optional[str]
) -> tuple[list[Finding], str]:
    """``_landing_test_gate_body`` with each ``targeted-tests-failed`` finding naming the peer runs whose
    uncommitted hunks sit in the run's declared paths (the measured-rev record's ``peer_hunks``)."""
    params = with_leg_record(worktree_root, params, run_id)
    findings, check = _landing_test_gate_body(worktree_root, params, run_id)
    if not findings:
        return findings, check
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

    owners = ltt.peer_hunk_owners(_measured_record(worktree_root, run_id))
    if not owners:
        return findings, check
    refusing, waived = _waive_on_peer_free_verdict(worktree_root, run_id, findings, owners)
    named = ltt.tag_findings_with_peer_owners(refusing, owners)
    if isinstance(findings, GateFindings):
        out = GateFindings(named)
        out.deleted_note, out.foreign_evidence = findings.deleted_note, findings.foreign_evidence
        out.unrun_owed = getattr(findings, "unrun_owed", ())
        out.unknown_ids = getattr(findings, "unknown_ids", ())
        out.peer_advisory = waived
        return out, (check if named or not waived else ltt.PEER_WAIVED_CHECK)
    if waived:
        out = GateFindings(named)
        out.peer_advisory = waived
        return out, (check if named else ltt.PEER_WAIVED_CHECK)
    return named, check


def _waive_on_peer_free_verdict(
    worktree_root: Path, run_id: Optional[str], findings: Sequence[Finding], owners: Mapping[str, str],
) -> tuple[list[Finding], list[Finding]]:
    """``(refusing, advisory)``: the reds on a file the run's overlay ran and passed move to advisory when
    every peer path is split out and the overlay verdict is green for this landing key. A red, timed-out,
    inconclusive, mismatched or missing verdict waives nothing."""
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

    measured = _measured_record(worktree_root, run_id)
    if not run_id or not ltt.peers_all_split(measured):
        return list(findings), []
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    verdict = cov.check_verdict(worktree_root, run_id, measured)
    if verdict.verdict != "green" or verdict.timed_out:
        return list(findings), []
    return ltt.waive_peer_reds(findings, owners, verdict.seeds)


def _unrun_nodes(verdict: Mapping, nodes: Sequence[str]) -> list[str]:
    """The failing ids of a carried ``verdict`` with no head measurement: what the retest must run."""
    return list(dict.fromkeys(unevidenced_ids(verdict.get("baseline") or {}, (), False))) or list(nodes)


def _landing_test_gate_body(
    worktree_root: Path, params: Mapping, run_id: Optional[str]
) -> tuple[list[Finding], str]:
    """``(findings, check)`` for a landing path: a valid fresh verdict replaces the recorded one.
    ``check`` is ``"pass-on-retest"`` when a fresh pass cleared it; otherwise as
    ``failed_targeted_tests`` (including ``"not-run"``, returned before any failure attribution).
    A None ``run_id`` skips the fresh read.

    A ``running`` retest record answers a ``RetestAwait`` (check ``"retest-running"``) with the poll
    command; ``landing_test_gate`` then skips the invariant check, since no verdict exists yet. A ``running``
    record past its deadline answers one ``RETEST_ABANDONED`` finding (check ``"failed"``) naming the
    record's tests ahead of the ``re-run:`` command.
    A recorded fail with no baseline names its failing ids from ``failed_tests`` (top
    level or under ``params.params``), the verdict's, and the task output's digest
    (``params.task_output_path``); with no baseline evidence each id's file is its own refusal (the
    red counts caused, never inferred pre-existing from a path) whose remedy retests that file. With no id known the refusal names the ``failed_tests`` param."""
    record = _retest_record(worktree_root, run_id) or {}
    if record.get("status") == RETEST_RUNNING:
        from coordinator_core.ops.dispatch_emit.halt_remedy_class import remedy_or_halt
        from coordinator_core.ops.dispatch_emit.landing_targeted_tests import _in_leg_attempt
        from coordinator_core.ops.dispatch_emit.retest_targeted import retest_abandoned

        if retest_abandoned(record):
            ids = [str(t).strip() for t in record.get("tests") or () if str(t).strip()]
            return [Finding(
                "", RETEST_ABANDONED,
                "the run's retest was abandoned past its deadline; " + "".join(f"{i}; " for i in ids)
                + "re-run: " + retest_remedy(params),
            )], "failed"
        await_detail = (
            "the run's retest is still running; poll: " + retest_remedy(params) + " --poll --wait"
        )
        return [remedy_or_halt(
            TARGETED_TESTS_FAILED, _in_leg_attempt,
            lambda _f: RetestAwait("", TARGETED_TESTS_FAILED, await_detail), attempts=1,
        )], "retest-running"
    fresh = fresh_targeted_verdict(worktree_root, run_id) if run_id else None
    leg = _zero_test_leg_record(worktree_root, run_id)
    if leg is not None and not (
        fresh is not None and fresh.get("status") in ("pass", "pass-with-skips") and not _ran_nothing(fresh.get("tests_run"))
    ):
        return [_ran_zero_finding(params, cause_note=_zero_cause_text(leg))], "failed"
    # `not in` (key absent) here: an empty failed_tests list still means the launcher named nothing,
    # unlike the truthiness test below.
    errored_unrun = (
        fresh is not None and fresh.get("status") == "error" and _ran_nothing(fresh.get("tests_run"))
        and FAILED_TESTS_PARAM not in fresh
    )
    if errored_unrun and _explicit_failed_tests(params):
        fresh = None
    elif errored_unrun:
        cause_note = zero_run_cause_note(fresh, retest_remedy(params))
        if cause_note:
            return _not_run_with_cause(cause_note), NOT_RUN_CHECK
        reason = fresh.get("reason") or "launcher produced no test results"
        return [tests_failed_finding("",
                        f"the run's retest errored without running tests ({reason}); "
                        f"fix the launcher and re-run: {retest_remedy(params)}")], "failed"
    unrun_retest = False
    empty_note = ""
    if fresh is not None and _ran_no_tests(fresh):
        unrun_retest = not _only_run_deleted_targets(worktree_root, params, run_id, fresh)
        empty_note = zero_run_cause_note(fresh, retest_remedy(params)) if unrun_retest else ""
        fresh = None
    if fresh is not None and not isinstance(fresh.get("baseline"), Mapping) and (
        fresh.get("status") == "fail"
        or (fresh.get("status") == "error" and fresh.get(FAILED_TESTS_PARAM))
    ):
        verdict, fresh = fresh, None
    else:
        verdict = None
    if fresh is None:
        verdict = verdict or targeted_test_verdict(params)
        check_now = failed_targeted_tests(verdict)[1]
        zero_run = check_now == NOT_RUN_CHECK or (
            check_now == "pass" and verdict.get("status") == "pass" and _ran_zero_int(verdict.get("tests_run"))
        )
        if empty_note and check_now == "no-verdict":
            return _not_run_with_cause(empty_note), NOT_RUN_CHECK
        if zero_run or (unrun_retest and check_now == "no-verdict"):
            if unrun_retest or _targeted_set_exists(worktree_root, params, verdict, run_id):
                return [_ran_zero_finding(params, _renamed_detail(worktree_root, params, run_id))], "failed"
            if _zero_cause_text(verdict):
                return [_ran_zero_finding(params, cause_note=_zero_cause_text(verdict))], "failed"
            if check_now == NOT_RUN_CHECK:
                return [], NOT_RUN_CHECK
        nodes = list(dict.fromkeys([
            *_failed_nodes(params, verdict), *_recorded_failed_nodes(worktree_root, run_id),
            *reverify_failing_nodes(params),
        ]))
        if nodes and failed_targeted_tests(verdict)[1] == ZERO_FAIL_CHECK:
            verdict = {**verdict, "tests_failed": None}
        files = list(dict.fromkeys(f for f in (_node_file(n) for n in nodes) if f))
        findings, check = failed_targeted_tests(verdict, retest_remedy(params, files), measured=False, nodes=nodes)
        if any(f.reason == REVIEW_REVERIFY_INDETERMINATE for f in findings):
            return findings, check
        if check == UNRUN_OWED:
            owed = GateFindings()
            owed.unrun_owed = _unrun_nodes(verdict, nodes)
            return owed, check
        if check != "failed" or verdict.get("baseline"):
            return findings, check
        if nodes and set(nodes) <= (base_red_ids(params) | sidecar_base_red(worktree_root, params)):
            attributed = GateFindings()
            attributed.foreign_evidence = {n: [FAILING_AT_BASE] for n in nodes}
            return attributed, "pre-existing-only"
        if not files:
            if _explicit_empty_failed_tests(params) and _retest_record_status(worktree_root, run_id) == "pass":
                return [], "pre-existing-only"
            # A measured clean re-verify outranks an unattributed carried fail (a stale or zero-run retest
            # record measured nothing); a failing re-verify names its nodes above and still refuses.
            if not nodes and reverify_passed_clean(params):
                return [], "pass-on-retest"
            return [Finding(findings[0].path, findings[0].reason,
                            f"{findings[0].detail}; or pass params.{FAILED_TESTS_PARAM} (failing node ids)"
                            f"{_stale_retest_note(worktree_root, run_id)}")], check
        if verdict.get("status") == "error" and _ran_nothing(verdict.get("tests_run")):
            # Trap: an errored retest that ran nothing measured no node, so its named ids are caused-unmeasured, not unknown.
            return findings, check
        unknown = GateFindings()
        base_red = base_red_ids(params) | sidecar_base_red(worktree_root, params)
        unknown.unknown_ids = list(dict.fromkeys(n for n in nodes if _node_file(n) and n not in base_red))
        return unknown, UNKNOWN_ONLY
    sidecar_red = sidecar_base_red(worktree_root, params)
    sidecar_red = sidecar_red | _base_measured_foreign(worktree_root, params, run_id, fresh)
    cleared = base_red_cleared(worktree_root, params, fresh) | sidecar_red
    findings, check = failed_targeted_tests(fresh, retest_remedy(params), cleared=cleared)
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

    base = fresh.get("baseline")
    if check in ("failed", UNKNOWN_ONLY) and isinstance(base, Mapping) and not base.get("caused"):
        owed = ltt.unevidenced_ids(base, cleared)
        passing = ltt.owed_check_cleared(fresh, owed) if owed else set()
        if passing:
            findings, check = failed_targeted_tests(fresh, retest_remedy(params), cleared=cleared | passing)
        left_ids = [n for n in owed if n not in passing]
        if left_ids and check == UNKNOWN_ONLY and ltt._measured_ids(fresh, ltt.OWED_CLEARED_KEY) is not None:
            # The retest measured the owed ids at HEAD + the run's landing paths and these did not come back
            # green: the run's own bytes are the only change there, so the red is caused.
            groups = ltt._group_by_file(left_ids)
            return [
                ltt.tests_failed_finding(
                    path, "; ".join(ns[:3]) + "; red at HEAD plus the run's landing paths (owed base check); "
                    f"re-run: {retest_remedy(params)}")
                for path, ns in groups.items()
            ], "failed"
        if owed and check == UNKNOWN_ONLY:
            unknown = GateFindings(findings)
            unknown.unknown_ids = left_ids
            return unknown, check
        if owed and check == "failed":
            left = len(owed) - len(passing)
            findings = [
                replace(f, detail=f"{f.detail}; owed base check (the retest record's HEAD + landing paths run): "
                                  f"{left} of {len(owed)} id(s) not cleared there (red or unmeasured; "
                                  f"re-run: {retest_remedy(params)})")
                if f.detail else f for f in findings
            ]
    base_note = ""
    base_red: set[str] = set()
    if check == "failed" and isinstance(base, Mapping) and base.get("caused"):
        base_red, base_note = ltt.base_check_red(fresh, [str(n) for n in base["caused"]])
        if not isinstance(base.get(ltt.HEAD_RED_KEY), list):
            base_note = f"{base_note}; re-run: {retest_remedy(params)}"
        if base_red:
            moved = {
                **base, "caused": [n for n in base["caused"] if str(n) not in base_red],
                "pre_existing": [*(base.get("pre_existing") or []), *sorted(base_red)],
            }
            findings, check = failed_targeted_tests(
                {**fresh, "baseline": moved}, retest_remedy(params), cleared=cleared | base_red)
    if check == "failed" and findings:
        # Trap: a caller-named base-red id counts only where the retest ran its file or its source reads HEAD
        # (`cleared`); otherwise it is unmeasured and a caused id never downgrades on the caller's word.
        red_now = {ltt.norm_node_id(n) for n in base_red | sidecar_red | (base_red_ids(params) & cleared)}
        refusing, reported = ltt.caused_only(findings, red_now)
        # A finding's detail names at most three ids per file; any caused id of that file outside base red keeps it refusing.
        unlisted = {
            ltt._node_file(ltt.norm_node_id(n)) for n in (base.get("caused") or [] if isinstance(base, Mapping) else [])
            if ltt.norm_node_id(n) not in red_now
        }
        refusing += [f for f in reported if f.path in unlisted]
        reported = [f for f in reported if f.path not in unlisted]
        if reported:
            carried = dict(getattr(findings, "foreign_evidence", None) or {})
            carried.update({
                n: [FAILING_AT_BASE] for f in reported for n in ltt._finding_node_ids(f)
            })
            attributed = GateFindings(refusing)
            attributed.foreign_evidence = carried
            if refusing:
                findings = attributed
            else:
                findings, check, base_red = attributed, "pre-existing-only", base_red | set(carried)
    if check == "failed" and base_note:
        noted = [replace(f, detail=f"{f.detail}; {base_note}") if f.detail else f for f in findings]
        if isinstance(findings, GateFindings):
            noted = GateFindings(noted)
            noted.foreign_evidence = findings.foreign_evidence
        findings = noted
    if check == "pre-existing-only" and base_red:
        attributed = GateFindings(findings)
        attributed.foreign_evidence = {n: [FAILING_AT_BASE] for n in sorted(base_red)}
        return attributed, check
    if check == "pass" and fresh.get("status") == "pass" and _ran_zero_int(fresh.get("tests_run")):
        return [_ran_zero_finding(params)], "failed"
    if check == "pre-existing-only" and sidecar_red:
        base = fresh.get("baseline") if isinstance(fresh.get("baseline"), Mapping) else {}
        named = {
            str(i.get("id") if isinstance(i, Mapping) else i)
            for key in ("pre_existing", "foreign", "unverified") for i in (base.get(key) or [])
        } & sidecar_red
        if named:
            attributed = GateFindings(findings)
            attributed.foreign_evidence = {n: [FAILING_AT_BASE] for n in sorted(named)}
            return attributed, check
    return findings, "pass-on-retest" if check == "pass" else check


def _base_measured_foreign(
    worktree_root: Path, params: Mapping, run_id: Optional[str], fresh: Mapping
) -> set[str]:
    """``baseline.foreign`` ids of an engine-written record whose ``baseline.base_sha`` is the run base:
    red when measured at the run base. Empty for a record without ``base_sha`` or measured elsewhere.
    Never spawns."""
    base = fresh.get("baseline")
    sha = base.get("base_sha") if isinstance(base, Mapping) else None
    if not (isinstance(sha, str) and sha and sha == run_base_for(worktree_root, run_id, params)):
        return set()
    return {str(i.get("id") if isinstance(i, Mapping) else i) for i in (base.get("foreign") or [])}


def _not_run_with_cause(note: str) -> GateFindings:
    """No findings; ``deleted_note`` carries why zero tests ran, so the not-run result is visible on the receipt."""
    out = GateFindings()
    out.deleted_note = note
    return out


def _recorded_failed_nodes(worktree_root: Path, run_id: Optional[str]) -> list[str]:
    """Failing node ids the run's retest record names, fresh or not (naming only, never clearing):
    its ``failed_tests`` and the ``export`` and ``live`` passes'. In-process; one record read."""
    record = _retest_record(worktree_root, run_id) or {}
    out: list[str] = []
    for holder in (record, record.get("export"), record.get("live")):
        raw = holder.get(FAILED_TESTS_PARAM) if isinstance(holder, Mapping) else None
        out += [str(n).strip() for n in raw if n] if isinstance(raw, list) else []
    return [n for n in dict.fromkeys(out) if n]


def _ran_zero_int(tests_run: object) -> bool:
    """True only for the integer 0 (an absent count is not a zero run)."""
    return isinstance(tests_run, int) and not isinstance(tests_run, bool) and tests_run == 0


def _ran_zero_finding(params: Mapping, renamed: str = "", cause_note: str = "") -> Finding:
    return tests_failed_finding("",
                   "the targeted tests ran 0 tests (nothing collected or run), so nothing "
                   f"is proven{renamed}{cause_note}; re-run: {retest_remedy(params)}")


TEST_LEG_RECORD = "test-leg.json"


def test_leg_record(worktree_root: Path, run_id: Optional[str]) -> Optional[dict]:
    """The run's ``test-leg.json`` the test leg wrote (beside ``measured-rev.json``, the same
    ``<RECORD_DIR_ROOT>/<run_id>`` the leg's ``--out`` names; a refire of the run rewrites it); None when the
    file is absent, unreadable or not an object."""
    if not run_id:
        return None
    from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

    try:
        record = json.loads((measured_rev_record_path(worktree_root, run_id).parent / TEST_LEG_RECORD)
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _zero_test_leg_record(worktree_root: Path, run_id: Optional[str]) -> Optional[dict]:
    """The run's ``test-leg.json`` when it observed no completed test: its ``tests_run`` is the integer 0 or is
    absent/null (a non-pytest leg whose counts were never read); None when the file is absent, unreadable, not an
    object, or the leg ran tests."""
    record = test_leg_record(worktree_root, run_id)
    if record is None:
        return None
    ran = record.get("tests_run")
    return record if _ran_zero_int(ran) or ran is None else None


def with_leg_record(worktree_root: Path, params: Mapping, run_id: Optional[str]) -> Mapping:
    """``params`` with the test verdict the carried params hold replaced by the leg's own ``test-leg.json``
    counts when that record shows tests run and the carried verdict (``targeted_tests`` and each
    ``inline_review.tests``) reports none run; the same object back otherwise. A carried verdict that
    already shows a run, and a record that ran nothing, are left to their own gates."""
    record = test_leg_record(worktree_root, run_id)
    ran = record.get("tests_run") if record else None
    if not (isinstance(ran, int) and not isinstance(ran, bool) and ran > 0):
        return params
    out = dict(params)
    changed = False
    verdict = targeted_test_verdict(params)
    if verdict is None or _ran_nothing(verdict.get("tests_run")):
        carried = params.get(TARGETED_TESTS_PARAM)
        out[TARGETED_TESTS_PARAM] = {
            **(carried if isinstance(carried, Mapping) else {}),
            **{k: record[k] for k in ("status", "tests_run", "tests_failed", "baseline") if k in record},
        }
        changed = True

    def _patched(review: object) -> object:
        nonlocal changed
        tests = review.get("tests") if isinstance(review, Mapping) else None
        if not isinstance(tests, Mapping) or not _ran_nothing(tests.get("run")):
            return review
        changed = True
        fields = {"run": ran, "failed": record.get("tests_failed"), "sidecar": record.get("sidecar_path")}
        merged = {**tests, "run": ran, **{k: v for k, v in fields.items() if v is not None}}
        if isinstance(record.get("status"), str):
            merged["status"] = "pass" if record["status"] == "pass-with-skips" else record["status"]
        return {**review, "tests": merged}

    if "inline_review" in params:
        out["inline_review"] = _patched(params["inline_review"])
    outcome = params.get("run_outcome")
    if isinstance(outcome, Mapping) and "inline_review" in outcome:
        out["run_outcome"] = {**outcome, "inline_review": _patched(outcome["inline_review"])}
    return out if changed else params


def _zero_cause_text(record: Optional[Mapping]) -> str:
    """``"; <cause>"`` for a zero-test result naming why (known ``zero_run_cause``, else ``reason``,
    ``summary`` or ``error`` text); ``""`` for a bare zero count, which carries no evidence a test was attempted."""
    if not isinstance(record, Mapping):
        return ""
    cause = record.get(ZERO_RUN_CAUSE_KEY)
    if isinstance(cause, str) and cause in _ZERO_RUN_CAUSES:
        text = record.get("reason") or record.get("summary") or record.get("note")
        return f"; {cause}: {_ZERO_RUN_CAUSES[cause]}" + (f" ({text})" if text else "")
    text = next((str(record[k]) for k in ("reason", "summary", "error") if record.get(k)), "")
    return f"; recorded cause: {text}" if text else ""


def _renamed_detail(worktree_root: Path, params: Mapping, run_id: Optional[str]) -> str:
    """``"; target X was renamed to Y, retarget Y"`` for each renamed dropped target; ``""`` when none."""
    record = _retest_record(worktree_root, run_id)
    renamed = renamed_dropped_targets(worktree_root, params, run_id, record) if record else {}
    return "".join(f"; target {t} was renamed to {n}, retarget {n}" for t, n in renamed.items())


def _targeted_set_exists(
    worktree_root: Path, params: Mapping, verdict: Optional[Mapping], run_id: Optional[str]
) -> bool:
    """True when the run had targeted tests: a retest record that names tests or dropped targets (a
    ``stale`` one, written when no set exists, does not count), a targeted set (``targeted_set``) or
    explicit ``failed_tests``. Only a run with none (a docs/state-only change) may read not-run."""
    record = _retest_record(worktree_root, run_id)
    if record is not None and record.get("status") != "stale" and (
        record.get("tests") or (record.get("dropped") and not _only_run_deleted_targets(worktree_root, params, run_id, record))
    ):
        return True
    return bool(_explicit_failed_tests(params) or targeted_set(params, verdict))


def import_closure_findings(
    worktree_root: Path, paths: Sequence[str], deleted: Sequence[str],
    landed_text: Optional[Callable[[str], Optional[str]]] = None,
) -> list[Finding]:
    """Findings(IMPORT_CLOSURE_UNTRACKED) for landing ``.py`` paths that import a module or name
    HEAD will lack once the landing commits. ``landed_text(rel)`` supplies the bytes the commit
    lands (None = absent); without it the worktree is judged. ``deleted`` paths read as absent.
    A detector exception is one finding naming it (fail closed). Zero spawns."""
    from coordinator_core.authoring_leaks import HeadView, import_closure

    land_deadline.check("landing_integrity.import_closure_findings")
    gate = [*paths, *deleted]
    deleted_set = set(deleted)
    text = landed_text
    if text is not None and deleted_set:
        inner = text

        def text(rel: str) -> Optional[str]:
            return None if rel in deleted_set else inner(rel)

    try:
        head = HeadView(worktree_root, import_closure.candidates(gate))
        found = import_closure.detect(Path(worktree_root), gate, head, landed_text=text)
    except Exception as exc:
        return [Finding("", IMPORT_CLOSURE_UNTRACKED, f"import-closure detector failed: {type(exc).__name__}: {exc}")]
    return [Finding(f.path, IMPORT_CLOSURE_UNTRACKED, f"{f.line}: {f.detail}") for f in found]


def lost_definition_pairs(
    worktree_root: Path, findings: Sequence, paths: Sequence[str], carried: Sequence[str] = (),
) -> dict[str, str]:
    """``{importer: target}`` over ``IMPORT_CLOSURE_UNTRACKED`` ``findings`` whose target ``.py``
    (tracked, modified in the worktree) is neither in ``paths`` nor ``carried``: the landing's caller
    reads a name only that uncommitted file defines. Reuses the detector's findings; no new scan."""
    from coordinator_core.authoring_leaks import import_closure

    pairs = import_closure.withheld_symbol_pairs(
        worktree_root, [(f.path, f.detail) for f in findings if f.reason == IMPORT_CLOSURE_UNTRACKED],
    )
    lands = {*paths, *carried}
    return {imp: tgt for imp, tgt in pairs.items() if tgt not in lands}


def lost_definition_findings(
    worktree_root: Path, paths: Sequence[str], deleted: Sequence[str], carried: Sequence[str] = (),
    landed_text: Optional[Callable[[str], Optional[str]]] = None,
) -> list[Finding]:
    """Findings(LOST_DEFINITION) for landing ``.py`` paths that read a module attribute defined only
    by an uncommitted file the landing leaves out: HEAD would hold a caller of a name it lacks.
    AST over the landed ``.py`` only (``import_closure_findings``); zero extra spawns."""
    found = import_closure_findings(worktree_root, paths, deleted, landed_text)
    pairs = lost_definition_pairs(worktree_root, found, paths, carried)
    return [Finding(f.path, LOST_DEFINITION, f.detail) for f in found if f.path in pairs]


def withheld_closure_findings(
    worktree_root: Path, paths: Sequence[str], deleted: Sequence[str], withheld: Sequence[str],
    landed_text: Optional[Callable[[str], Optional[str]]] = None,
) -> list[Finding]:
    """``import_closure_findings`` over the landing set minus ``withheld``: a withheld path stays out
    of the gate, so the detector reads it at HEAD (a withheld new file is absent, a modified one
    keeps its HEAD names) and an importer of a name only the withheld bytes bind is a finding.
    Zero spawns."""
    out = set(withheld)
    return import_closure_findings(
        worktree_root, [p for p in paths if p not in out], [p for p in deleted if p not in out], landed_text,
    )


def since_base_findings(
    worktree_root: Path, paths: Sequence[str], base_sha: str, *, run_id: Optional[str],
    plan: Optional[str] = None,
) -> list[Finding]:
    """Findings for landing blobs that drop lines a foreign commit added since ``base_sha``;
    ``plan`` lets the run's own re-fire checkpoints read as own."""
    from coordinator_core.git import since_base_overlap

    land_deadline.check("landing_integrity.since_base_findings")
    unique = list(dict.fromkeys(paths))
    if not unique:
        return []  # the commit walk to base is the cost; no declared path, no walk
    found = list(since_base_overlap.find_overlaps(worktree_root, unique, base_sha, run_id=run_id, plan=plan))
    return found + _unreadable_pack_index_findings(worktree_root, unique)


def _unreadable_pack_index_findings(worktree_root: Path, paths: Sequence[str]) -> list[Finding]:
    """Findings(UNREADABLE_PACK_INDEX) for declared paths whose HEAD object cannot be located: an
    absent object reads as "no overlap", so a pack index that does not parse must surface here
    instead of passing the since-base check silently."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit
    from coordinator_core.git.since_base_overlap import _head_sha

    root = Path(worktree_root)
    try:
        common_dir = resolve_git_common_dir(root)
        head = _head_sha(root)
        if not head:
            return []
        lost = []
        for path in paths:
            blob = _blob_at_commit(common_dir, head, path)
            if blob is ABSENT:
                continue
            if blob is None or git_objects.read_object_header(common_dir, blob) is None:  # type: ignore[arg-type]
                lost.append(path)
        if not lost:
            return []
        bad = git_objects.unreadable_pack_indexes(common_dir)
    except OSError:
        return []
    if not bad:
        return []
    names = ", ".join(bad[:3]) + (f" (+{len(bad) - 3} more)" if len(bad) > 3 else "")
    return [
        Finding(path, UNREADABLE_PACK_INDEX, f"HEAD object not locatable; unreadable pack index: {names}")
        for path in lost
    ]
