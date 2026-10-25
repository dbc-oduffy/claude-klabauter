"""The halted-run commit: ``dispatch.terminal_commit`` given ``halted_by``.

A halted engine-ask run leaves its ``state/warp/<run_id>/`` record directory behind; this lands it
as one ``ceremony.commit_v2`` call carrying that directory's top-level files, the run's
``run_artifacts`` candidate set (sizing, baton, plan, evidence sidecar, slice sizings, superseded
record dirs, claimed docs-stage paths), the baton the run recorded (left open, ``status_reason``
stamped with the halt), the declared writes of every chunk the run finished, and
the halt reason in the message. Candidates are named paths, never the dirty tree. A finished chunk's tracked
deletion is declared, a deletion git cannot see is dropped, and a path a live peer's claim holds
is withheld and named. Only a ``ledger-check`` halt carrying a test verdict lands chunk product, because a resumed run counts
only that halt's ``Lands-Chunks`` (``emit.resume_landed_rows``) and re-runs every other halt's
chunks; any other halt, and a ``ledger-check`` halt with no verdict (``targeted_tests.check``
``no-verdict``), lands the record and run artifacts only (``halt-record-only``). A
``ledger-check`` halt lands a chunk only with its import closure: a chunk whose Python or TS/JS
imports would be missing at the commit, transitively, is withheld whole (``import-closure-open``).
A chunk owning a ``.py`` that fails to parse (``py-unparseable``) or whose landing blob drops lines a
foreign commit added since the run's base (``since-base-overlap``) is withheld whole the same way,
and so is a chunk any of whose changed declared writes is withheld for another reason
(``chunk-write-set-partial``): a chunk lands whole or not at all. A carried test verdict naming a
run-caused failure withholds the chunk owning the failing test file, or every chunk when the
failure is unattributed or in a file no landing chunk owns (``targeted-tests-failed``); the reply's
``targeted_tests.check`` says ``no-verdict`` when none was carried, and ``not-run`` (halt-record-only
too) when the carried verdict executed no test.
With no finished chunk the commit is record-only. No receipt, sizing ship
or close job runs; the one plan stamp codes this halt's ``Lands-Chunks`` rows at its sha. The run
token is only checked (``run_token.admits``), never claimed: no branch, raise or killed op can strand
it, so a halt never spends the token the resumed run's product commit needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from coordinator_core import dispatch_abandon
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import process_artifact_policy, review_edits, run_artifacts
from coordinator_core.ops.warp_run_deliverable import record_sizing_rel
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.worktree_snapshot import record_dir_changes
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.row_returns import CONSUMED_RETURNS_PARAM
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_DEPENDENTS_UNSTARTED,
    HALT_REASON_FIELDS,
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_OUTCOME_FILE,
    halted_record_message,
    write_run_outcome,
)
from coordinator_core.ops.dispatch_emit.land_run_halt import (
    _settle_ledger_pointer, held_rows_for, marker_kind, stamp_halt_lands_chunks,
)


def is_halted_record_call(params: object) -> bool:
    """True when ``params[HALTED_RECORD_PARAM]`` is a dict with a non-empty str ``halted``."""
    halted = params.get(HALTED_RECORD_PARAM) if isinstance(params, dict) else None
    return isinstance(halted, dict) and isinstance(halted.get("halted"), str) and bool(halted["halted"])


def within_handoffs_home(worktree_root: Path, artifacts: run_artifacts.RunArtifacts) -> run_artifacts.RunArtifacts:
    """``artifacts`` without any ``baton`` candidate outside the handoffs home: a sizing's
    ``baton:`` naming another directory is never landed."""
    from coordinator_core.session.record_homes import home_dir

    home = [Path(home_dir(str(worktree_root), "handoffs"))]
    kept = tuple(
        p for p in artifacts.by_class.get("baton", ()) if contained_path(worktree_root / p, home) is not None
    )
    if kept == artifacts.by_class.get("baton", ()):
        return artifacts
    return run_artifacts.RunArtifacts(
        run_id=artifacts.run_id,
        by_class={**artifacts.by_class, "baton": kept},
        record_dirs=artifacts.record_dirs,
        root=artifacts.root,
    )


def _run_request(worktree_root: Path, script_path: object):
    """The commit request an emitted script (or its ask-run manifest) promises, plus the ids the
    manifest withheld: ``(request, withheld_ids)``. ``(None, set())`` when nothing is readable."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, StageManifest
    from coordinator_core.ops.dispatch_emit.commit_request import (
        MalformedCommitRequestError,
        parse_manifest_marker,
        parse_marker,
    )

    if not isinstance(script_path, str) or not script_path:
        return None, set()
    script = contained_path(worktree_root / script_path, [worktree_root])
    if script is None:
        return None, set()
    try:
        text = script.read_text(encoding="utf-8")
        manifest_rel = parse_manifest_marker(text)
        if manifest_rel is None:
            return parse_marker(text), set()
        manifest_abs = contained_path(
            worktree_root / manifest_rel, [worktree_root / RECORD_DIR_ROOT, worktree_root / RUN_DIR_ROOT]
        )
        if manifest_abs is None:
            return None, set()
        manifest = StageManifest.from_json(json.loads(manifest_abs.read_text(encoding="utf-8")))
        marker = contained_path(worktree_root / manifest.marker_path, [worktree_root / RUN_DIR_ROOT])
        if marker is None:
            return None, set()
        withheld = {g.id for g in manifest.gated} | {o.id for o in manifest.owed}
        return parse_marker(marker.read_text(encoding="utf-8")), withheld
    except (OSError, ValueError, KeyError, TypeError, MalformedCommitRequestError):
        return None, set()


def done_chunks(worktree_root: Path, params: dict) -> list:
    """The chunks a halted run finished. With ``params.incomplete_chunks`` the rest are done, as in
    the DAG mode; without it a chunk is done only when its own report reads DONE and not
    PARTIAL/BLOCKED. A manifest-withheld chunk and an unreadable request are never done."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import (
        _DELIVERED_REPORT_RE,
        _UNDELIVERED_REPORT_RE,
        _read_rel,
    )

    request, withheld = _run_request(worktree_root, params.get("script_path"))
    if request is None:
        return []
    listed = params.get("incomplete_chunks")
    incomplete = set(listed) if isinstance(listed, list) else None
    out: list = []
    for chunk in request.chunks:
        if chunk.id in withheld or (incomplete is not None and chunk.id in incomplete):
            continue
        if incomplete is None:
            text = _read_rel(worktree_root, chunk.report) if chunk.report else None
            if text is None or not _DELIVERED_REPORT_RE.search(text) or _UNDELIVERED_REPORT_RE.search(text):
                continue
        out.append(chunk)
    return out


def _held_reason(halted: dict) -> Optional[str]:
    """The halt's first non-empty reason field, whitespace collapsed; None when it carries none."""
    for field in HALT_REASON_FIELDS:
        text = " ".join(str(halted.get(field) or "").split())
        if text:
            return text
    return None


def _reap_staged_twins(worktree_root: Path, run_id: str, *, own: bool) -> None:
    """Sweep expired staged twins always; drop the halted run's own twins only when ``own`` (its run
    token verified). Zero spawns; never fails the commit. Trap: an unverified caller must never pass
    ``own=True``, since a valid-shaped run_id alone would delete a live run's twin."""
    from coordinator_core.ops.dispatch_emit import launcher_staged_twin

    try:
        if own:
            launcher_staged_twin.remove_twins(worktree_root, run_id)
        launcher_staged_twin.sweep_expired_twins(worktree_root)
    except Exception:  # noqa: BLE001 -- cleanup must not fail the commit
        pass


def commit_halted_record(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error

    halted = params[HALTED_RECORD_PARAM]
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if contained_path(worktree_root / RECORD_DIR_ROOT / run_id, [worktree_root / RECORD_DIR_ROOT]) is None:
        return _error(f"record dir for {run_id!r} escapes {RECORD_DIR_ROOT}", refused="record-dir-escapes")
    record_dir_rel = f"{RECORD_DIR_ROOT}/{run_id}"

    from coordinator_core.ops.dispatch_emit.run_close import sibling_release

    siblings = sibling_release(worktree_root, run_id, None)
    token = params.get("run_token")
    script_path = params.get("script_path")
    verified = token is not None and run_token.admits(worktree_root, run_id, token, script_path)
    _reap_staged_twins(worktree_root, run_id, own=verified)
    if token is not None and not verified:
        refusal = _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
        if siblings is not None:
            refusal["siblings"] = siblings
        return refusal
    reply = _land(worktree_root, repo_root, params, run_id, record_dir_rel, halted)
    if token is not None:
        reply = _reconcile_landing(worktree_root, run_id, token, script_path, halted, reply)
    if siblings is not None:
        reply["siblings"] = siblings
    return reply


def _reconcile_landing(
    worktree_root: Path, run_id: str, token: object, script_path: object, halted: dict, reply: dict
) -> dict:
    """Record a landed record commit beside the live token; answer a re-run that finds nothing left
    to commit as ``already_landed`` with the recorded sha when it is on HEAD and the halt kind is the
    same. The token is never consumed or released here, and a re-run lands no second commit."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
    from coordinator_core.ops.dispatch_emit.terminal_commit_reconcile import landed_on_head

    kind = halted.get("halted")
    if reply.get("committed") and isinstance(reply.get("sha"), str):
        run_token.note_halt_landing(
            worktree_root, run_id, token, script_path,
            {"sha": reply["sha"], "paths": list(reply.get("paths") or []), "halted": kind},
        )
        return reply
    if reply.get("committed") is False and reply.get("nothing_to_commit") and not reply.get("error"):
        landed = run_token.halt_landing(worktree_root, run_id, token, script_path)
        sha = landed.get("sha") if landed else None
        same_halt = bool(landed) and landed.get("halted") == kind
        if same_halt and isinstance(sha, str) and landed_on_head(worktree_root, sha):
            answer = {k: v for k, v in reply.items() if k != "nothing_to_commit"}
            return {**answer, "committed": True, "sha": sha, ALREADY_LANDED_KEY: True,
                    "paths": list(landed.get("paths") or [])}
    return reply


#: Evidence kept per blocking item, in UTF-8 bytes, so the marker stays small.
_EVIDENCE_CAP_BYTES = 2000


def _capped(text: str) -> str:
    return text.encode("utf-8")[:_EVIDENCE_CAP_BYTES].decode("utf-8", "ignore")


def _blocking_items(items: list) -> list:
    out = []
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("id"), str):
            continue
        rem = i.get("remaining")
        evidence = i.get("evidence")
        out.append({
            "id": i["id"],
            "remaining": [r for r in rem if isinstance(r, str)] if isinstance(rem, list) else [],
            "evidence": _capped(evidence) if isinstance(evidence, str) else "",
        })
    return out


def _ledger_halt_plan_and_blocking(repo_root: Path, halted: dict) -> tuple:
    """(plan_rel, blocking ids, blocking items) of a halt naming a relative, contained plan;
    (None, (), []) otherwise. Each item is `{id, remaining, evidence}` with strings only."""
    plan_rel = halted.get(HALT_PLAN_FIELD)
    if not isinstance(plan_rel, str) or not plan_rel or Path(plan_rel).is_absolute():
        return None, (), []
    if contained_path(repo_root / plan_rel, [repo_root]) is None:
        return None, (), []
    raw = halted.get("items")
    items = _blocking_items(raw if isinstance(raw, list) else [])
    return plan_rel, tuple(i["id"] for i in items), items


def _xs_spine_halt(worktree_root: Path, record_dir_rel: str, halted: dict) -> tuple:
    """A plan-less ledger-check halt's (spine_rel, blocking ids, blocking items), the spine being
    the single ``*.spine.md`` in the run's record dir; (None, (), []) when it holds none or several."""
    from coordinator_core.ops.dispatch_emit.ask_contract import SPINE_SUFFIX

    spines = sorted((worktree_root / record_dir_rel).glob(f"*{SPINE_SUFFIX}"))
    if len(spines) != 1:
        return None, (), []
    raw = halted.get("items")
    items = _blocking_items(raw if isinstance(raw, list) else [])
    return f"{record_dir_rel}/{spines[0].name}", tuple(i["id"] for i in items), items


_OUTCOME_HALT_KINDS = ("commit", "ledger-check")


def _persist_run_outcome(repo_root: Path, run_id: str, halted: dict, params: dict) -> Optional[str]:
    """Write the run's review outcome into its record dir for an all-landed commit or ledger-check
    halt (a dict ``params.run_outcome`` with a dict ``inline_review`` and no incomplete chunk);
    returns its repo-relative posix path, else None."""
    outcome = params.get("run_outcome")
    if halted.get("halted") not in _OUTCOME_HALT_KINDS or not isinstance(outcome, dict):
        return None
    if not isinstance(outcome.get("inline_review"), dict) or outcome.get("incomplete_chunks"):
        return None
    write_run_outcome(repo_root, run_id, outcome)
    return f"{RECORD_DIR_ROOT}/{run_id}/{RUN_OUTCOME_FILE}"


def _live_run_ids(repo_root: Path) -> list:
    """Run ids holding a live or claimed token; a read failure names none."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    try:
        return [r["run_id"] for r in abandoned_runs._token_records(repo_root)]
    except Exception:
        return []


HALT_RUN_REASON = "halted-run"


def _review_written(worktree_root: Path, session_id: Optional[str], run_id: str) -> Optional[dict]:
    """Review-stage writes since the run started; None when the source cannot answer."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _run_started_at

    try:
        return review_edits.review_writes(worktree_root, session_id, _run_started_at(worktree_root, run_id))
    except Exception:
        return None


#: ``withheld`` reason for a path this run wrote that stays uncommitted and no other leg named.
RUN_DIRT_UNCOMMITTED = "run-dirt-uncommitted"


def _run_written(worktree_root: Path, session_id: Optional[str], run_id: str) -> list:
    """Worktree-relative paths the session or any agent it dispatched wrote since the run started,
    from the engine's touch records (executors included, unlike the review-stage source). Empty
    when the source cannot answer: unknown never names a path, so a peer's dirt is never claimed."""
    import os

    from coordinator_core.ops.dispatch_emit.terminal_commit import _run_started_at
    from coordinator_core.session import core, touch_record

    try:
        since = _run_started_at(worktree_root, run_id)
        hub = core.sessions_dir(str(worktree_root))
        if not session_id or since is None or not hub:
            return []
        session_dir = os.path.join(hub, session_id)
        sinks = [session_dir]
        try:
            ledger = Path(session_dir, "dispatched-agents.txt").read_text(encoding="utf-8")
        except FileNotFoundError:
            ledger = ""
        for row in ledger.splitlines():
            fields = row.split("\t")
            agent_dir = os.path.join(hub, ".agents", fields[0]) if fields[0] else None
            if agent_dir and len(fields) >= 3 and touch_record.agent_owner_sid(agent_dir)[0] == session_id:
                sinks.append(agent_dir)
        found: set = set()
        for sink in dict.fromkeys(sinks):
            for member in touch_record.discover_family(touch_record.sink_path(sink)):
                try:
                    raw = member.read_bytes()
                except FileNotFoundError:
                    continue
                for line in touch_record.iter_complete_lines(raw):
                    try:
                        event = touch_record.decode_line(line)
                    except touch_record.MalformedRecordLine:
                        continue
                    if (
                        event.verb == touch_record.VERB_TOUCH and event.kind != touch_record.KIND_READ
                        and event.timestamp >= since
                    ):
                        rel = review_edits._norm(event.path)
                        if not review_edits._out_of_worktree(rel) and not rel.startswith("scratch/"):
                            found.add(rel)
        return sorted(found)
    except Exception:  # noqa: BLE001 -- an unreadable source names no path
        return []


def _withhold_run_dirt(withheld: dict, written: list, changed: set, landed: set) -> None:
    """Add to ``withheld`` each path in ``written`` that is still changed against HEAD, not landed
    by this commit and not already named; never a path the run did not write."""
    for p in written:
        if p in changed and p not in landed and p not in withheld:
            withheld[p] = RUN_DIRT_UNCOMMITTED


def _integrity_findings(
    worktree_root: Path, run_id: str, base: Optional[str], paths: list, plan: Optional[str] = None
) -> list:
    """Parse and since-base-overlap findings over ``paths``; the overlap arm is skipped when the
    run has no recorded ``base``."""
    from coordinator_core.ops.dispatch_emit import landing_integrity

    found = landing_integrity.unparseable_py(worktree_root, paths)
    if base is not None and paths:
        found += landing_integrity.since_base_findings(
            worktree_root, paths, base, run_id=run_id, plan=plan
        )
    return found


def _stamp_run_baton(
    worktree_root: Path, run_id: str, kind: str, reason: Optional[str]
) -> tuple[Optional[str], Optional[dict]]:
    """Stamp the baton the run recorded with ``status_reason`` naming the halt; ``status`` and
    ``deployment_state`` stay as they are, so a refire still picks it up. Returns
    ``(baton_rel, error)``: ``baton_rel`` is the stamped path (None when the run recorded no
    readable baton inside the handoffs home), ``error`` is ``{path, error}`` for a refused write."""
    from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
    from coordinator_core.frontmatter.primitives import insert_fm_field, read_fm_field, replace_fm_field
    from coordinator_core.locked_write import LockTimeout, locked_rmw
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton
    from coordinator_core.session.record_homes import home_dir

    rel = read_run_baton(worktree_root, run_id)
    if rel is None:
        return None, None
    abs_path = contained_path(worktree_root / rel, [Path(home_dir(str(worktree_root), "handoffs"))])
    if abs_path is None or not abs_path.is_file():
        return None, None
    value = f"run {run_id} halted: {kind}" + (f": {reason}" if reason else "")

    def compose(old: str) -> str:
        split = split_frontmatter(old)
        if split is None:
            raise ValueError("baton has no frontmatter")
        fm = split.fm_text
        if read_fm_field(fm, "status_reason") is None:
            fm = insert_fm_field(fm, "status_reason", value)
        else:
            fm = replace_fm_field(fm, "status_reason", value)
        return old if fm == split.fm_text else rebuild(split, fm)

    try:
        locked_rmw(abs_path, compose, repo_root=worktree_root)
    except (LockTimeout, OSError, ValueError) as exc:
        return rel, {"path": rel, "error": str(exc)}
    return rel, None


def _baton_cascade(worktree_root: Path, repo_root: Path, run_id: str) -> dict:
    """``{"run_baton_cascade": ...}`` for the run's recorded baton, ``{}`` when it recorded none.
    The op fires only for a baton already terminal; no headroom before the dispatch deadline
    records ``op-unavailable`` and calls nothing."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton

    rel = read_run_baton(worktree_root, run_id)
    if rel is None:
        return {}
    left = dispatch_abandon.remaining()
    if left is not None and left - dispatch_abandon.REPLY_MARGIN_SECS <= 0:
        return {"run_baton_cascade": {"fired": [], "skipped": [{"baton": rel, "reason": "op-unavailable"}]}}
    from coordinator_core.ops.dispatch_emit.run_baton_cascade import fire_for_batons

    return {"run_baton_cascade": fire_for_batons(worktree_root, repo_root, [rel], None)}


def _manifest_doc(record_dir: Path) -> dict:
    """The run manifest as a dict; empty when absent or unreadable."""
    import json

    try:
        data = json.loads((record_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_baseline(record_dir: Path, doc: Optional[dict] = None) -> Optional[dict]:
    """The run manifest's ``write_baseline`` (read from ``doc`` when given); None when the manifest or the key is absent or unreadable."""
    base = (_manifest_doc(record_dir) if doc is None else doc).get("write_baseline")
    return base if isinstance(base, dict) and base else None


def _post_checkpoint_candidates(
    root: Path, record_dir_rel: str, doc: dict, excluded_owners: dict, review_literals: list,
    run_written: list,
) -> list:
    """Paths this run declared or wrote that a halt may need to keep: manifest row writes (minus those
    still equal to ``write_baseline``), ``review_declared_paths``, review and touch-record writes, and
    the record dir's prior ``post_checkpoint`` paths. Never a record-dir, ``excluded_owners``, scratch
    or out-of-worktree path."""
    from coordinator_core.ops.dispatch_emit import kept_work, write_baseline

    declared: list = []
    for row in doc.get("rows") or ():
        writes = row.get("writes") if isinstance(row, dict) else None
        declared.extend(w for w in writes or () if isinstance(w, str))
    declared.extend(p for p in doc.get("review_declared_paths") or () if isinstance(p, str))
    declared = [review_edits._norm(p) for p in declared]
    baseline = _manifest_baseline(root / record_dir_rel, doc)
    stale = set(write_baseline.unwritten(root, baseline, declared)) if baseline else set()
    prior = kept_work.read(root / record_dir_rel)[kept_work.POST_CHECKPOINT_KEY]
    owned = {review_edits._norm(p) for p in excluded_owners}
    out: dict = {}
    for raw in (*[p for p in declared if p not in stale], *review_literals, *run_written, *prior):
        rel = review_edits._norm(raw)
        if (
            not rel or review_edits._out_of_worktree(rel) or rel == "scratch" or rel.startswith("scratch/")
            or rel.startswith(record_dir_rel + "/") or rel in owned
        ):
            continue
        out[rel] = None
    return list(out)


def _coded_rows(worktree_root: Path, halted: dict) -> tuple:
    """``(row ids the halt's plan marks coded, error)``. The set is empty when the plan field is
    absent (error None) or the plan cannot be read or decoded (error names it): an unreadable plan
    credits no row, and the caller surfaces that."""
    from coordinator_core.ops.dispatch_emit import delivery_credit

    plan = halted.get(HALT_PLAN_FIELD)
    if not isinstance(plan, str) or not plan:
        return set(), None
    try:
        by_ref = delivery_credit.coded_row_refs(str(worktree_root / plan))
    except (OSError, ValueError) as exc:
        return set(), f"{plan}: {exc}"
    return {i for ids in by_ref.values() for i in ids}, None


def _manifest_kept_from(record_dir: Path) -> dict:
    """``{row id: prior run id}`` for each row the run manifest stages ``kept_from``; empty when unreadable."""
    import json

    try:
        data = json.loads((record_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("rows") if isinstance(data, dict) else None
    return {
        r["id"]: r["kept_from"]
        for r in rows or ()
        if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("kept_from"), str) and r["kept_from"]
    }


def _carried_returns(root: Path, record_dir: Path, returns: dict) -> dict:
    """``returns`` with each kept-from-prior-run row's text replaced by that run's persisted entry
    (a kept row's reply is a stub); a row with no such entry keeps its own text."""
    from coordinator_core.ops.dispatch_emit import row_returns

    out = dict(returns)
    for row, prior in _manifest_kept_from(record_dir).items():
        if row not in out or not safe_id(prior):
            continue
        entry = row_returns.read(root / RECORD_DIR_ROOT / prior).get(row)
        if entry is not None:
            out[row] = entry["text"]
    return out


def _persist_blobs(root: Path, common_dir: Optional[Path], post: dict) -> set:
    """Write each ``post`` path's bytes as a git blob (in-process, no spawn) when its sha still
    equals the recorded fingerprint, so a reviewed hunk stays recoverable after a refire; returns the
    paths whose blob was written. A path whose read or write raises OSError or whose bytes changed is
    left out; any other exception from ``write_object`` propagates. ``write_object`` is
    content-addressed, so a lost race is not a lost write."""
    from coordinator_core.git import git_objects
    from coordinator_core.ops.dispatch_emit import kept_work

    written: set = set()
    if common_dir is None or not common_dir.is_dir():
        return written
    for rel, want in post.items():
        try:
            data = (root / rel).read_bytes()
            if kept_work.blob_sha(data) != want:
                continue
            git_objects.write_object(common_dir, b"blob", data)
        except OSError:
            continue
        written.add(rel)
    return written


def _record_kept(
    root: Path, record_dir_rel: str, owner: dict, withheld: dict, halted: dict, paths: list,
    consumed_returns: Optional[dict] = None, post_candidates: Iterable[str] = (), landed: Iterable[str] = (),
    common_dir: Optional[Path] = None, manifest_doc: Optional[dict] = None,
) -> dict:
    """Write ``kept.json`` beside the withheld map and ``returns.json`` beside it when
    ``consumed_returns`` (row id to reply) holds a str entry; append their paths to ``paths`` (in
    place) so they land with the record, and stamp ``kept.json`` on the marker as
    ``next_action.resume.stage``. ``post_candidates`` that are in ``withheld``, not in ``landed`` and
    owned by no kept chunk are fingerprinted under ``post_checkpoint`` when their blob was persisted;
    the rest are named under ``post_checkpoint_unpersisted``. Every kept chunk path's blob is
    persisted too; those that could not be are named under ``kept_unpersisted``.
    Returns ``{"kept": {...}}`` (plus ``"returns": [ids]``) when anything was recorded, else ``{}``."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import kept_work, row_returns

    record_dir = root / record_dir_rel
    if not record_dir.is_dir():
        return {}
    if common_dir is None:
        common_dir = resolve_git_common_dir(root)
    returns = {
        k: v for k, v in (consumed_returns.items() if isinstance(consumed_returns, dict) else ())
        if isinstance(k, str) and isinstance(v, str)
    }
    persisted: dict = {}
    if returns:
        returns = _carried_returns(root, record_dir, returns)
        if row_returns.write(record_dir, returns) is not None:
            persisted = {k: v["sha256"] for k, v in row_returns.read(record_dir).items()}
            returns_rel = row_returns.persisted_path(record_dir_rel)
            if returns_rel not in paths:
                paths.append(returns_rel)
    owning = set(owner.values())
    baseline = _manifest_baseline(record_dir, manifest_doc)
    chunks = kept_work.kept_chunks(
        root, owner, withheld, baseline,
        returns=persisted, finished=[r for r in persisted if r not in owning],
    )
    gate = kept_work.plan_gate(halted, root)
    recorded = {p for entry in chunks.values() for p in entry["paths"]} | set(owner)
    wanted = {
        p: blob
        for p, blob in kept_work.post_checkpoint_hunks(
            root, post_candidates, landed=landed, recorded=recorded, baseline=baseline
        ).items()
        if p in withheld
    }
    stored = _persist_blobs(root, common_dir, wanted)
    post = {p: blob for p, blob in wanted.items() if p in stored}
    kept_paths = {p: fp for entry in chunks.values() for p, fp in entry["paths"].items()}
    kept_stored = _persist_blobs(root, common_dir, kept_paths)
    written = kept_work.write(record_dir, chunks, gate, post)
    reply: dict = {"returns": sorted(persisted)} if persisted else {}
    if len(kept_stored) != len(kept_paths):
        reply["kept_unpersisted"] = sorted(set(kept_paths) - kept_stored)
    if len(post) != len(wanted):
        reply["post_checkpoint_unpersisted"] = sorted(set(wanted) - set(post))
    if written is None:
        return reply
    rel = f"{record_dir_rel}/{kept_work.KEPT_FILE}"
    if rel not in paths:
        paths.append(rel)
    kept = {"chunks": sorted(chunks), "plan_gate": gate is not None}
    if post:
        kept[kept_work.POST_CHECKPOINT_KEY] = sorted(post)
    stage = {"kept": rel, **kept}
    if persisted:
        stage["returns"] = sorted(persisted)
    halted_run.record_resume_stage(record_dir, stage)
    return {"kept": kept, **reply}


def _land(
    worktree_root: Path, repo_root: Path, params: dict, run_id: str, record_dir_rel: str, halted: dict
) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.credit_gate import KEPT_REVIEW_CHUNK_ID
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths
    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops.dispatch_emit.terminal_commit import _own_prefix_files, _tracked_at_head
    from coordinator_core.ops.dispatch_emit import landing_integrity

    base = {"record_dir": record_dir_rel, "halted": halted["halted"]}
    halt_reason = _held_reason(halted) if halted["halted"] == HALT_DEPENDENTS_UNSTARTED else None
    run_base = landing_integrity.run_base_for(worktree_root, run_id, params)
    session_id: Optional[str] = params.get("session_id")
    if not (isinstance(session_id, str) and _UUID_RE.fullmatch(session_id)):
        session_id = None

    finished = done_chunks(worktree_root, params)
    declared: dict = {}  # path -> owning chunk id, first declarer wins
    report_cache: dict = {}
    for chunk in finished:
        if chunk.id == KEPT_REVIEW_CHUNK_ID:
            continue
        for path in list(chunk.paths) + (_own_prefix_files(worktree_root, chunk, report_cache) or []):
            declared.setdefault(path, chunk.id)
    owner = declared
    home, _siblings = split_sibling_paths(list(owner), worktree_root)
    declared = [p for p in home if not p.startswith(record_dir_rel + "/")]
    sizing_rel = record_sizing_rel(worktree_root, run_id)
    if (
        sizing_rel
        and not Path(sizing_rel).is_absolute()
        and contained_path(worktree_root / sizing_rel, [worktree_root]) is not None
        and sizing_rel not in owner
        and not sizing_rel.startswith(record_dir_rel + "/")
    ):
        declared.append(sizing_rel)

    outcome_rel = _persist_run_outcome(worktree_root, run_id, halted, params)
    plan_rel = None
    if (worktree_root / record_dir_rel).is_dir():
        plan_rel, blocking, blocking_items = (
            _ledger_halt_plan_and_blocking(repo_root, halted)
            if marker_kind(halted) == "ledger-check" else (None, (), [])
        )
        if halted["halted"] == "ledger-check" and plan_rel is None:
            plan_rel, blocking, blocking_items = _xs_spine_halt(
                worktree_root, record_dir_rel, halted
            )
        write_halt_marker(
            worktree_root / record_dir_rel,
            halted=marker_kind(halted),
            sizing_rel=sizing_rel,
            plan_rel=plan_rel,
            blocking=blocking if plan_rel else None,
            blocking_items=blocking_items if plan_rel else None,
            run_base_sha=run_base,
            reason=halt_reason,
            next_action=halted.get("next_action") if isinstance(halted.get("next_action"), dict) else None,
            held_rows=held_rows_for(worktree_root, run_id, plan_rel),
        )
    raw_docs = params.get(run_artifacts.DOC_PATHS_PARAM)
    artifacts = within_handoffs_home(worktree_root, run_artifacts.candidate_paths(
        worktree_root, run_id,
        sizing_rel=sizing_rel,
        plan_rel=halted.get(HALT_PLAN_FIELD) if isinstance(halted.get(HALT_PLAN_FIELD), str) else None,
        doc_paths=[d for d in raw_docs if isinstance(d, str)] if isinstance(raw_docs, list) else (),
    ))
    artifacts, process_withheld = process_artifact_policy.withhold_process_artifacts(worktree_root, artifacts)
    if process_withheld:
        declared = [p for p in declared if p != sizing_rel]
        if sizing_rel:
            process_withheld[sizing_rel] = process_artifact_policy.WITHHELD_REASON
    baton_rel, baton_error = _stamp_run_baton(worktree_root, run_id, halted["halted"], halt_reason)
    if process_withheld:
        if baton_rel is not None:
            process_withheld[baton_rel] = process_artifact_policy.WITHHELD_REASON
    elif baton_rel is not None and baton_rel not in artifacts.by_class.get("baton", ()) and baton_rel not in declared:
        declared.append(baton_rel)
    baton_reply: dict = {"run_baton": baton_error} if baton_error is not None else {}
    review_written = _review_written(worktree_root, session_id, run_id)
    review_literals = [] if review_written is None else [
        p for p in review_written if not review_edits._out_of_worktree(review_edits._norm(p))
    ]
    run_written = _run_written(worktree_root, session_id, run_id)
    request, _ = _run_request(worktree_root, params.get("script_path"))
    request_owned = {p: c.id for c in (request.chunks if request is not None else ()) for p in c.paths}
    coded, coded_error = _coded_rows(worktree_root, halted)
    coded_reply: dict = {"coded_rows_unreadable": coded_error} if coded_error else {}
    uncoded_dirt = [
        review_edits._norm(p) for p, cid in request_owned.items() if p not in owner and cid not in coded
    ]
    manifest_doc = _manifest_doc(worktree_root / record_dir_rel)
    post_candidates = _post_checkpoint_candidates(
        worktree_root, record_dir_rel, manifest_doc,
        {**{p: c for p, c in request_owned.items() if c not in coded}, **owner},
        review_literals, run_written,
    )
    listed = declared + list(artifacts.paths()) + review_literals + run_written
    snapshot = _worktree_snapshot(
        worktree_root, listed + [p for p in [*post_candidates, *uncoded_dirt] if p not in listed],
        [record_dir_rel, RECORD_DIR_ROOT], [],
    )
    if snapshot is None:
        return _error(f"git status over {record_dir_rel} failed", **base)
    artifacts = artifacts.with_superseded(
        run_artifacts.superseded_runs(
            worktree_root, run_id, sizing_rel, snapshot.changed, live_run_ids=_live_run_ids(repo_root)
        ),
        RECORD_DIR_ROOT,
    )
    artifacts, more_withheld = process_artifact_policy.withhold_process_artifacts(worktree_root, artifacts)
    process_withheld.update(more_withheld)
    paths, deleted = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)
    if process_withheld:
        process_withheld.update({p: process_artifact_policy.WITHHELD_REASON for p in (*paths, *deleted)})
        paths, deleted = [], []

    product: list = []
    gone: list = []
    for path in declared:
        if path not in snapshot.changed:
            continue
        (product if (worktree_root / path).is_file() else gone).append(path)
    tracked_gone = set(_tracked_at_head(worktree_root, gone))
    dropped = [{"path": p, "reason": "deletion-git-cannot-see"} for p in gone if p not in tracked_gone]
    gone = [p for p in gone if p in tracked_gone]
    withheld: dict = dict(process_withheld)

    handled = set(paths) | set(deleted) | set(declared)
    own_record = {p for p in artifacts.paths() if artifacts.class_of(p) == "record-dir"}
    extra_artifacts = run_artifacts.landable(
        worktree_root,
        run_artifacts.RunArtifacts(
            run_id=run_id,
            by_class={
                cls: tuple(p for p in ps if p not in handled and p not in own_record)
                for cls, ps in artifacts.by_class.items()
            },
            record_dirs=artifacts.record_dirs,
            root=artifacts.root,
        ),
        snapshot.changed, (), session_id=session_id,
    )
    withheld.update(extra_artifacts.withheld)
    art_paths = list(extra_artifacts.present)
    art_deleted = list(extra_artifacts.deleted)

    from coordinator_core.ops.dispatch_emit import sibling_claims

    sibling = sibling_claims.sibling_run_claims(
        worktree_root, run_id, product + gone + art_paths + art_deleted + review_literals
    )
    if sibling:
        own_declared = set(owner) | set(paths) | set(deleted) | own_record
        if sizing_rel:
            own_declared.add(sizing_rel)
        product, w1 = sibling_claims.filter_own(product, sibling, own_declared)
        gone, w2 = sibling_claims.filter_own(gone, sibling, own_declared)
        art_paths, w3 = sibling_claims.filter_own(art_paths, sibling, own_declared)
        art_deleted, w4 = sibling_claims.filter_own(art_deleted, sibling, own_declared)
        withheld.update({p: sibling_claims.SIBLING_WITHHELD_REASON for p in (*w1, *w2, *w3, *w4)})

    holds: list = []
    warnings = commit_v2._peer_claim_warnings(
        worktree_root, product + gone + art_paths + art_deleted, holds, own_sid=session_id
    )
    held = {p for p, _sid in holds}
    withheld.update({p: "peer-held" for p in sorted(held)})
    peer_held_by: dict = {}
    if holds:
        from coordinator_core.ops.dispatch_emit import peer_claim_contract

        peer_held_by = {"peer_held_by": peer_claim_contract.group(holds)}
    # Warning text is commit_v2._peer_claim_warnings' contract: "claim state indeterminate (<exc>)" or "... for '<path>'".
    indeterminate = [w for w in warnings if w.startswith("claim state indeterminate")]
    if indeterminate:
        # Only declared product paths are withheld on an indeterminate claim; run artifacts (record, sizing) still land.
        everywhere = any(w.startswith("claim state indeterminate (") for w in indeterminate)
        for p in product + gone:
            if p not in held and (everywhere or f"claim state indeterminate for {p!r}" in indeterminate):
                withheld[p] = "peer-claim-indeterminate"
    pending = product + gone
    product = [p for p in product if p not in withheld]
    gone = [p for p in gone if p not in withheld]
    art_paths = [p for p in art_paths if p not in withheld]
    art_deleted = [p for p in art_deleted if p not in withheld]
    closure_open: dict = {}
    integrity: list = []
    targeted: Optional[dict] = None
    tfind: list = []
    tcheck = ""
    if halted["halted"] == "ledger-check":
        tfind, tcheck = landing_integrity.landing_test_gate(worktree_root, params, run_id)
        targeted = {
            "check": tcheck,
            "findings": [{"path": f.path, "reason": f.reason, "detail": f.detail} for f in tfind],
        }
    if halted["halted"] != "ledger-check":
        withheld.update({p: "halt-record-only" for p in product + gone if p in owner})
    elif product or gone:
        from coordinator_core.authoring_leaks import import_closure, ts_import_closure
        from coordinator_core.ops.dispatch_emit import halt_closure

        # Trap: no verdict, or a not-run verdict (no test executed), on a ledger-check halt means the
        # scoped-test leg never ran, so landing would ship untested product; a resumed run re-runs
        # the withheld chunks and its terminal commit carries the verdict.
        if tcheck in ("no-verdict", landing_integrity.NOT_RUN_CHECK):
            withheld.update({p: "halt-record-only" for p in product + gone if p in owner})
            product = [p for p in product if p not in withheld]
            gone = [p for p in gone if p not in withheld]

        partial = halt_closure.partial_chunks(owner, pending, product + gone)
        withheld.update({p: "chunk-write-set-partial" for p in product + gone if owner.get(p) in partial})
        product = [p for p in product if p not in withheld]
        gone = [p for p in gone if p not in withheld]

        closure_open = halt_closure.open_chunks(
            worktree_root, owner, product + gone + art_paths, (import_closure, ts_import_closure)
        )
        withheld.update({p: "import-closure-open" for p in product + gone if owner.get(p) in closure_open})
        plan = halted.get("plan") if isinstance(halted.get("plan"), str) else None
        if not plan:
            marker, _ = _run_request(worktree_root, params.get("script_path"))
            plan = getattr(marker, "plan_path", None)
        integrity = _integrity_findings(
            worktree_root, run_id, run_base, [p for p in product if p not in withheld], plan
        )
        if integrity:
            bad_chunks = {owner[f.path]: f.reason for f in reversed(integrity) if f.path in owner}
            withheld.update({
                p: bad_chunks[owner[p]] for p in product + gone
                if p not in withheld and owner.get(p) in bad_chunks
            })
        if tfind:
            landing = {p for p in product + gone if p not in withheld}
            landing_chunks = {owner[p] for p in landing if p in owner}
            hit = {owner[f.path] for f in tfind if f.path in owner and owner[f.path] in landing_chunks}
            everything = any(f.path not in owner or owner[f.path] not in landing_chunks for f in tfind)
            withheld.update({
                p: landing_integrity.TARGETED_TESTS_FAILED for p in landing
                if p in owner and (everything or owner[p] in hit)
            })
    product = [p for p in product if p not in withheld]
    gone = [p for p in gone if p not in withheld]
    unlanded_chunks = {owner[p] for p in withheld if p in owner}
    landed_chunks = sorted({owner[p] for p in product + gone if p in owner} - unlanded_chunks)
    paths = paths + product + art_paths
    deleted = deleted + gone + art_deleted
    landed_classes: dict = {}
    for p in art_paths + art_deleted:
        cls = artifacts.class_of(p)
        if cls:
            landed_classes[cls] = landed_classes.get(cls, 0) + 1
    review_reply: dict = {}
    unlanded: dict = {}
    review_landed: list = []
    if review_written is None:
        review_reply["review_edits"] = None
    else:
        verdict = review_edits.partition(
            review_written, changed=snapshot.changed, ignored=snapshot.ignored,
            committed=set(paths) | set(deleted), sibling_declared=sibling, contested=set(),
            present=lambda p: (worktree_root / p).is_file(),
        )
        unlanded = {p: HALT_RUN_REASON for p in sorted(verdict.halt)}
        stageable = [p for p in verdict.land if p != "scratch" and not p.startswith("scratch/") and p not in withheld]
        review_holds: list = []
        review_warnings = commit_v2._peer_claim_warnings(
            worktree_root, stageable, review_holds, own_sid=session_id
        )
        review_held = {p for p, _sid in review_holds}
        for p in stageable:
            if p in review_held:
                unlanded[p] = "peer-held"
                withheld[p] = "peer-held"
            elif any(
                w.startswith("claim state indeterminate")
                and (w.startswith("claim state indeterminate (") or f"claim state indeterminate for {p!r}" in w)
                for w in review_warnings
            ):
                unlanded[p] = "peer-claim-indeterminate"
                withheld[p] = "peer-claim-indeterminate"
            else:
                review_landed.append(p)
        unlanded.update({p: HALT_RUN_REASON for p in verdict.land if p not in stageable})
        paths = paths + [p for p in review_landed if p not in paths]
        if review_landed:
            landed_classes["review-edit"] = len(review_landed)
    review_reply["review_edits_unlanded"] = unlanded

    # Trap: taken before _record_kept appends kept.json/returns.json to ``paths``; the record files are
    # never post-checkpoint candidates, so they must not count as landed here.
    landed_now = set(paths) | set(deleted)
    _withhold_run_dirt(withheld, [*run_written, *post_candidates, *uncoded_dirt], snapshot.changed, landed_now)

    if process_withheld:
        process_artifact_policy.exclude_locally(worktree_root, process_withheld, artifacts.record_dirs)

    if not paths and not deleted:
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
        return {
            "committed": False, "sha": None, "nothing_to_commit": True,
            **({"withheld": withheld} if withheld else {}), **peer_held_by,
            **({"targeted_tests": targeted} if targeted is not None else {}), **review_reply,
            **baton_reply, **coded_reply, **_baton_cascade(worktree_root, repo_root, run_id), **base,
        }

    if (worktree_root / record_dir_rel).is_dir():
        halted_run.record_withheld(worktree_root / record_dir_rel, withheld)
    kept_reply = _record_kept(
        worktree_root, record_dir_rel, owner, withheld, halted, paths, params.get(CONSUMED_RETURNS_PARAM),
        post_candidates=[p for p in post_candidates if p in snapshot.changed], landed=landed_now,
        common_dir=resolve_git_common_dir(worktree_root), manifest_doc=manifest_doc,
    )
    message = halted_record_message(run_id, halted)
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_halted_trailer_lines

    for line in run_halted_trailer_lines(landing_integrity.closing_bases(worktree_root, run_id, params)):
        message += "\n" + line
    for path in unlanded:
        message += "\nReview-Edit-Unlanded: " + path
    for path in (kept_reply.get("kept") or {}).get(kept_work.POST_CHECKPOINT_KEY, ()):
        message += "\nKept-Post-Checkpoint: " + path
    if landed_chunks:
        message += "\nLands-Chunks: " + ", ".join(landed_chunks)
    if landed_classes:
        message += "\nLands-Artifacts: " + ", ".join(f"{c}={n}" for c, n in sorted(landed_classes.items()))
    if deleted:
        message += "\nRemoves: " + ", ".join(deleted)
    commit_params: dict = {"paths": paths, "message": message, "carry_stopped_runs": False}
    if deleted:
        commit_params["deleted_paths"] = deleted
        commit_params["declared_reverts"] = deleted
    if product or art_paths or review_landed:
        commit_params["ignored_untracked"] = snapshot.ignored_among(paths)
    if session_id is not None:
        commit_params["session_id"] = session_id
    left = dispatch_abandon.remaining()
    if left is not None and left - dispatch_abandon.REPLY_MARGIN_SECS <= 0:
        return _error(
            f"ceremony.commit_v2 step not started: {left:.1f}s left before the dispatch deadline; "
            "nothing committed",
            **base,
        )
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
    except OpUnavailableError:
        return _error("ceremony.commit_v2 is not registered", **base)
    if not isinstance(reply, dict):
        return _error(f"unexpected commit_v2 reply: {reply!r}", **base)
    extra: dict = {**review_reply, **peer_held_by, **baton_reply, **coded_reply, **kept_reply}
    if not reply.get("error"):
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
        extra.update(_baton_cascade(worktree_root, repo_root, run_id))
        left = run_artifacts.residue(
            artifacts, set(snapshot.changed) - set(paths) - set(deleted), paths + deleted, withheld
        )
        if left:
            extra["run_artifact_residue"] = left
        halt_sha = reply.get("sha") if reply.get("committed") else None
        stamp = stamp_halt_lands_chunks(
            worktree_root, repo_root, halted, plan_rel, landed_chunks, halt_sha, session_id,
        )
        if stamp is not None:
            extra["lands_chunks_stamp"] = stamp
    extra["run_artifacts"] = {"landed": sorted(paths + deleted), "withheld": withheld}
    if landed_chunks:
        extra["landed_chunks"] = landed_chunks
    if withheld:
        extra["withheld"] = withheld
    if closure_open:
        extra["closure_open"] = closure_open
    if dropped:
        extra["dropped"] = dropped
    if targeted is not None:
        extra["targeted_tests"] = targeted
    if integrity:
        extra["integrity"] = [{"path": f.path, "reason": f.reason, "detail": f.detail} for f in integrity]
    return {**reply, "paths": paths + deleted, **extra, **base}  # paths: everything the commit names
