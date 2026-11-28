"""Evaluate one typed stage_wait blocker (live, cleared, dead, pending) by file reads only."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NamedTuple, Optional

from coordinator_core.ops.dispatch_emit import co_tenant_owner, halted_run, pending_oracle, run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_DEPENDENTS_UNSTARTED, RECORD_DIR_ROOT, record_dir

LIVE = "live"
CLEARED = "cleared"
DEAD = "dead"
PENDING = "pending"
STATES = (LIVE, CLEARED, DEAD, PENDING)

_SHA = re.compile(r"[0-9a-f]{40}")
_VERDICTS_MAX = 512
_VERDICTS: dict = {}

_REMOTE_CLEARED = frozenset({"ended", "landed", "cleared", "done"})
_REMOTE_DEAD = frozenset({"halted", "failed", "dead"})
_REMOTE_LIVE = frozenset({"running", "live"})


class Verdict(NamedTuple):
    """`(state, payload)`; `cleared` and `dead` read the state for callers that test a flag."""

    state: str
    payload: dict

    @property
    def cleared(self) -> bool:
        return self.state == CLEARED

    @property
    def dead(self) -> bool:
        return self.state == DEAD


def _stat_sig(path: Optional[Path]) -> Optional[tuple]:
    if path is None:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _run_end(root: Path, owner: str, home: Optional[Path] = None) -> Verdict:
    """The owner's end state; a halted, token-less run keeps its verdict while its token record and `halted.json` keep their stat signature."""
    key = (str(root), owner)
    live_path = run_token._record_path(root, owner, home=home)
    held = _VERDICTS.get(key)
    if held is not None and held[0] == (_stat_sig(live_path), _stat_sig(held[1])):
        return held[2]
    try:
        live = run_token.is_live(root, owner)
        if live and not co_tenant_owner._launcherless_halted(root, owner):
            return Verdict(LIVE, {})
        rdir = record_dir(root, owner)
        marker = halted_run.read_strict(rdir)
    except (ValueError, OSError):
        return Verdict(LIVE, {})
    if marker is None:
        return Verdict(CLEARED, {})
    status = marker.get("status")
    if status == halted_run.STATUS_RESUMED:
        return Verdict(PENDING, {})
    sizing_rel = marker.get("sizing_rel")
    if status == halted_run.STATUS_SUPERSEDED and co_tenant_owner._landed_successor(root, owner, sizing_rel):
        return Verdict(CLEARED, {})
    if status not in (halted_run.STATUS_HALTED, halted_run.STATUS_SUPERSEDED):
        return Verdict(CLEARED, {})
    if not (isinstance(sizing_rel, str) and sizing_rel):
        return Verdict(CLEARED, {})
    marker_path = rdir / halted_run.HALTED_FILE
    try:
        mtime = marker_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    halt = marker.get("halted")
    payload = {"halted": halt, "sizing_rel": sizing_rel, "marker_mtime": mtime}
    dead = status == halted_run.STATUS_SUPERSEDED or halt == HALT_DEPENDENTS_UNSTARTED
    verdict = Verdict(DEAD if dead else LIVE, payload)
    if not live and status == halted_run.STATUS_HALTED and rdir.parent.as_posix().endswith(RECORD_DIR_ROOT):
        if len(_VERDICTS) >= _VERDICTS_MAX:
            _VERDICTS.clear()
        _VERDICTS[key] = ((_stat_sig(live_path), _stat_sig(marker_path)), marker_path, verdict)
    return verdict


def _run_landed(root: Path, run_id: str) -> Verdict:
    """Cleared only on a `landed` marker carrying a 40-hex `sha`, returned as payload; an unreadable or sha-less marker is pending."""
    try:
        marker = halted_run.read_strict(record_dir(root, run_id))
    except (OSError, ValueError):
        return Verdict(PENDING, {})
    if marker is None or marker.get("status") != halted_run.STATUS_LANDED:
        return _run_end_when_unlanded(root, run_id, marker)
    sha = marker.get("sha")
    if isinstance(sha, str) and _SHA.fullmatch(sha):
        return Verdict(CLEARED, {"sha": sha})
    return Verdict(PENDING, {})


def _run_end_when_unlanded(root: Path, run_id: str, marker: Optional[dict]) -> Verdict:
    """A run that has not landed: still running or marker trailing is pending; a dead run is dead, and it never lands."""
    verdict = _run_end(root, run_id)
    if verdict.state == DEAD:
        return verdict
    return Verdict(PENDING, {})


def _file_state(root: Path, blocker: Mapping) -> Verdict:
    """`ref` is a repo-relative path; `expect` is `exists` (default) or `absent`; `mtime_after` (epoch seconds) additionally requires a newer mtime."""
    ref = str(blocker["ref"]).replace("\\", "/")
    pure = PurePosixPath(ref)
    if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", ref):
        return Verdict(PENDING, {"reason": "path-not-repo-relative"})
    try:
        st = (Path(root) / pure).stat()
    except OSError:
        st = None
    expect = blocker.get("expect", "exists")
    if expect == "absent":
        return Verdict(CLEARED if st is None else LIVE, {})
    if st is None:
        return Verdict(LIVE, {})
    after = blocker.get("mtime_after")
    if isinstance(after, (int, float)) and not isinstance(after, bool) and st.st_mtime <= after:
        return Verdict(LIVE, {})
    return Verdict(CLEARED, {"mtime": st.st_mtime})


def _oracle_check(root: Path, blocker: Mapping) -> Verdict:
    """Cleared when the PM flag for (`ref` run id, `row`) is absent; `row` names the flag's row id."""
    row = blocker.get("row")
    if not isinstance(row, str) or not row:
        return Verdict(PENDING, {"reason": "row-missing"})
    try:
        flag = pending_oracle.flag_path(root, str(blocker["ref"]), row)
    except (OSError, ValueError):
        return Verdict(PENDING, {})
    return Verdict(LIVE if flag.exists() else CLEARED, {})


def remote_result_path(root: Path, ref: str) -> Optional[Path]:
    """Where the background drain writes a `remote_run` blocker's cached result, beside the wait records; None for an unsafe ref."""
    directory = stage_wait._wait_dir(root)
    if directory is None or not re.fullmatch(r"[A-Za-z0-9._-]+", ref or ""):
        return None
    return directory / f"remote-{ref}.json"


def _remote_run(root: Path, blocker: Mapping) -> Verdict:
    """Maps the cached result's `status` (`ended|landed|cleared|done`, `halted|failed|dead`, `running|live`); no result yet is pending."""
    import json

    path = remote_result_path(root, str(blocker["ref"]))
    if path is None:
        return Verdict(PENDING, {})
    try:
        result: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Verdict(PENDING, {})
    status = result.get("status") if isinstance(result, dict) else None
    if status in _REMOTE_CLEARED:
        return Verdict(CLEARED, {k: v for k, v in result.items() if k == "sha"})
    if status in _REMOTE_DEAD:
        return Verdict(DEAD, {"status": status})
    if status in _REMOTE_LIVE:
        return Verdict(LIVE, {})
    return Verdict(PENDING, {})


def evaluate(root: Path, blocker: Mapping) -> Verdict:
    """`(state, payload)` of one typed blocker: `live` (still running), `cleared`, `dead` (will never clear) or `pending` (undecidable now)."""
    root = Path(root)
    kind = blocker.get("kind", "run_end")
    ref = blocker.get("ref")
    if not isinstance(ref, str) or not ref:
        return Verdict(PENDING, {})
    if kind == "run_end":
        return _run_end(root, ref)
    if kind == "run_landed":
        return _run_landed(root, ref)
    if kind == "file_state":
        return _file_state(root, blocker)
    if kind == "oracle_check":
        return _oracle_check(root, blocker)
    if kind == "remote_run":
        return _remote_run(root, blocker)
    raise ValueError(f"blocker_check: unknown blocker kind {kind!r}; one of {stage_wait.BLOCKER_KINDS}")
