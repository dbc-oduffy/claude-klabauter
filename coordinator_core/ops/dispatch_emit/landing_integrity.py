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
import re
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

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
    RETEST_LAUNCHER,
    REVERIFY_INDETERMINATE,
    REVERIFY_KEY,
    REVIEW_REVERIFY_FAILED,
    REVIEW_REVERIFY_INDETERMINATE,
    TARGETED_TESTS_FAILED,
    TARGETED_TESTS_PARAM,
    ZERO_FAIL_CHECK,
    ZERO_FAIL_UNATTRIBUTED,
    Finding,
    tests_failed_finding,
    _FILE_LIKE,
    _GATE_BLOCKER,
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
    recorded_targeted_set,
    retest_remedy,
    reverify_record,
    reverify_red_findings,
    reverify_verdict,
    targeted_set,
    targeted_test_verdict,
)

PY_UNPARSEABLE = "py-unparseable"
SINCE_BASE_OVERLAP = "since-base-overlap"
IMPORT_CLOSURE_UNTRACKED = "import-closure-untracked"

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
    out: list[Finding] = []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        land_deadline.check("landing_integrity.unparseable_py")
        try:
            source = (Path(worktree_root) / rel).read_bytes()
        except OSError:
            continue
        found = _parse_finding(rel, source)
        if found:
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
    data, pos, out = proc.stdout_bytes, 0, []
    for rel in wanted:
        eol = data.find(b"\n", pos)
        if eol < 0:
            break
        header = data[pos:eol].split()
        pos = eol + 1
        if len(header) != 3 or header[1] != b"blob":
            continue
        size = int(header[2])
        found = _parse_finding(rel, data[pos : pos + size])
        pos += size + 1
        if found:
            out.append(found)
    return out


def closing_bases(worktree_root: Path, run_id: Optional[str], params: Mapping) -> tuple:
    """The 40-hex bases a run's closing or halt trailer names, deduplicated, in order: the stage
    manifest ``head_sha``, then ``run_base_for``, then ``params["review_run_base"]``.

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
    return tuple(dict.fromkeys(b for b in found if isinstance(b, str) and re.fullmatch(r"[0-9a-f]{40}", b)))


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
    (``""`` when none). A plain list otherwise, so ``== []`` and unpacking keep working."""

    deleted_note: str = ""
    foreign_evidence: Optional[dict] = None


def landing_test_gate(
    worktree_root: Path, params: Mapping, run_id: Optional[str],
    landed_paths: Optional[Sequence[str]] = None,
) -> tuple[GateFindings, str]:
    """``_landing_test_gate_core`` plus the invariant gate: with ``landed_paths`` given, every
    repo-wide invariant whose measured set they touch must be in the run's fresh passing retest,
    else one ``TARGETED_TESTS_FAILED`` finding per missing invariant (check ``"failed"``)."""
    findings, check = _landing_test_gate_core(worktree_root, params, run_id)
    if not landed_paths:
        return findings, check
    foreign: dict = {}
    owed = invariant_findings(worktree_root, landed_paths, run_id, params, foreign)
    carried = findings.foreign_evidence or {}
    if not owed:
        if foreign:
            findings.foreign_evidence = {**carried, **foreign}
        return findings, check
    out = GateFindings([*findings, *owed])
    out.deleted_note = findings.deleted_note
    out.foreign_evidence = {**carried, **foreign}
    return out, "failed"


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
    return list(imp.existing_invariants(worktree_root, imp.invariants_for_paths(landed_paths, worktree_root)))


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


def invariant_findings(
    worktree_root: Path, landed_paths: Sequence[str], run_id: Optional[str], params: Mapping,
    foreign_out: Optional[dict] = None,
) -> list[Finding]:
    """One finding per repo-wide invariant a landing diff touches that no fresh passing retest
    ran. An invariant node red in a fresh ``fail`` retest and in its ``baseline.evidence`` (red on a
    pure-HEAD export) is credited foreign, not a finding; ``foreign_out`` receives ``{node: evidence}``.
    Table lookup plus one record read; zero spawns. Paths other halted runs withhold
    (``halted_hold_release.residue_paths``) are residue at HEAD, not this run's diff: they select no
    invariant. Residue bytes are never touched."""
    owed = landing_invariant_nodes(worktree_root, landed_paths, run_id)
    if not owed:
        return []
    fresh = fresh_targeted_verdict(worktree_root, run_id) if run_id else None
    ran = set(fresh.get("tests") or []) if fresh and fresh.get("status") == "pass" else set()
    foreign: set[str] = set()
    if fresh and fresh.get("status") == "fail" and isinstance(fresh.get("baseline"), Mapping):
        from coordinator_core.completion_receipts.verdict import evidenced_clear_ids

        # Trap: trusted only because fresh_targeted_verdict returned an engine-written record that
        # matches this tree's fingerprint; a carried record must pass measured=False.
        foreign = evidenced_clear_ids(fresh["baseline"], measured=True)
    red, only_base_red = _red_nodes(fresh, foreign)
    out = []
    for node in owed:
        if node in ran or node.split("::")[0] in ran:
            continue
        if only_base_red and node not in red:
            continue
        if node in foreign:
            if foreign_out is not None:
                foreign_out[node] = [e for e in (fresh["baseline"].get("evidence") or []) if e == node]
            continue
        from coordinator_core.ops.dispatch_emit.landing_targeted_tests import retests_spent

        actual = sorted(red - {node}) if node not in red else []
        spent = retests_spent(params)
        if spent and actual:
            detail = (
                f"repo-wide invariant {node} did not fail; the in-run retest failed on "
                f"{', '.join(actual[:3])}"
            )
        elif spent and fresh is None:
            detail = f"repo-wide invariant {node}: no fresh retest verdict exists after the in-run retest"
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
            detail = (
                f"repo-wide invariant {node} measures a path this landing touches and no fresh passing "
                f"retest ran it; re-run: {retest_remedy(params, [node])}"
            )
        out.append(tests_failed_finding(node.split("::")[0], detail))
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
        out.deleted_note = deleted_targets_receipt_note(worktree_root, params, run_id)
    return out, check


def _landing_test_gate(
    worktree_root: Path, params: Mapping, run_id: Optional[str]
) -> tuple[list[Finding], str]:
    """``(findings, check)`` for a landing path: a valid fresh verdict replaces the recorded one.
    ``check`` is ``"pass-on-retest"`` when a fresh pass cleared it; otherwise as
    ``failed_targeted_tests`` (including ``"not-run"``, returned before any failure attribution).
    A None ``run_id`` skips the fresh read.

    A ``running`` retest record refuses (check ``"retest-running"``) with the poll command.
    A recorded fail with no baseline names its failing ids from ``failed_tests`` (top
    level or under ``params.params``), the verdict's, and the task output's digest
    (``params.task_output_path``); with no baseline evidence each id's file is its own refusal (the
    red counts caused, never inferred pre-existing from a path) whose remedy retests that file. With no id known the refusal names the ``failed_tests`` param."""
    if _retest_record_status(worktree_root, run_id) == RETEST_RUNNING:
        return [tests_failed_finding("",
                        "the run's retest is still running; poll: " + retest_remedy(params)
                        + " --poll; if poll reports it abandoned past deadline, re-run: "
                        + retest_remedy(params))], "retest-running"
    fresh = fresh_targeted_verdict(worktree_root, run_id) if run_id else None
    # `not in` (key absent) here: an empty failed_tests list still means the launcher named nothing,
    # unlike the truthiness test below.
    errored_unrun = (
        fresh is not None and fresh.get("status") == "error" and _ran_nothing(fresh.get("tests_run"))
        and FAILED_TESTS_PARAM not in fresh
    )
    if errored_unrun and _explicit_failed_tests(params):
        fresh = None
    elif errored_unrun:
        reason = fresh.get("reason") or "launcher produced no test results"
        return [tests_failed_finding("",
                        f"the run's retest errored without running tests ({reason}); "
                        f"fix the launcher and re-run: {retest_remedy(params)}")], "failed"
    unrun_retest = False
    if fresh is not None and _ran_no_tests(fresh):
        unrun_retest = not _only_run_deleted_targets(worktree_root, params, run_id, fresh)
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
        if zero_run or (unrun_retest and check_now == "no-verdict"):
            if unrun_retest or _targeted_set_exists(worktree_root, params, verdict, run_id):
                return [_ran_zero_finding(params, _renamed_detail(worktree_root, params, run_id))], "failed"
            if check_now == NOT_RUN_CHECK:
                return [], NOT_RUN_CHECK
        nodes = list(dict.fromkeys([*_failed_nodes(params, verdict), *_recorded_failed_nodes(worktree_root, run_id)]))
        if nodes and failed_targeted_tests(verdict)[1] == ZERO_FAIL_CHECK:
            verdict = {**verdict, "tests_failed": None}
        files = list(dict.fromkeys(f for f in (_node_file(n) for n in nodes) if f))
        findings, check = failed_targeted_tests(verdict, retest_remedy(params, files), measured=False, nodes=nodes)
        if any(f.reason == REVIEW_REVERIFY_INDETERMINATE for f in findings):
            return findings, check
        if check != "failed" or verdict.get("baseline"):
            return findings, check
        if nodes and set(nodes) <= base_red_ids(params):
            attributed = GateFindings()
            attributed.foreign_evidence = {n: [FAILING_AT_BASE] for n in nodes}
            return attributed, "pre-existing-only"
        if not files:
            if _explicit_empty_failed_tests(params) and _retest_record_status(worktree_root, run_id) == "pass":
                return [], "pre-existing-only"
            return [Finding(findings[0].path, findings[0].reason,
                            f"{findings[0].detail}; or pass params.{FAILED_TESTS_PARAM} (failing node ids)"
                            f"{_stale_retest_note(worktree_root, run_id)}")], check
        return [
            tests_failed_finding(f,
                    "; ".join([n for n in nodes if _node_file(n) == f][:3])
                    + f"; unevidenced red (not shown red pre-change), counted caused; "
                    f"re-run: {retest_remedy(params, [f])}")
            for f in files
        ], "failed"
    findings, check = failed_targeted_tests(
        fresh, retest_remedy(params), cleared=base_red_cleared(worktree_root, params, fresh))
    if check == "pass" and fresh.get("status") == "pass" and _ran_zero_int(fresh.get("tests_run")):
        return [_ran_zero_finding(params)], "failed"
    return findings, "pass-on-retest" if check == "pass" else check


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


def _ran_zero_finding(params: Mapping, renamed: str = "") -> Finding:
    return tests_failed_finding("",
                   "the targeted tests ran 0 tests (nothing collected or run), so nothing "
                   f"is proven{renamed}; re-run: {retest_remedy(params)}")


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


def since_base_findings(
    worktree_root: Path, paths: Sequence[str], base_sha: str, *, run_id: Optional[str],
    plan: Optional[str] = None,
) -> list[Finding]:
    """Findings for landing blobs that drop lines a foreign commit added since ``base_sha``;
    ``plan`` lets the run's own re-fire checkpoints read as own."""
    from coordinator_core.git import since_base_overlap

    land_deadline.check("landing_integrity.since_base_findings")
    return list(since_base_overlap.find_overlaps(worktree_root, paths, base_sha, run_id=run_id, plan=plan))
