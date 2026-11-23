"""The one verdict on "may this caller start this test run now", shared by the suite guard and the
heavy-admission guard. Spawn-free: payload fields, the session touch record and the two readings
guard_heavy_command_admission already takes (RAM floor, box hold).

Legs run in order: identity (a subagent other than the test-runner is refused every pytest shape),
scope (the test-runner names test files or node ids, no workers; an untouched target is an advisory), ceiling (every caller,
the EM included, while the box is below its RAM floor or held by another session). A reading that
cannot be taken allows and logs an advisory (DR-402); the identity leg reads payload fields only and
always runs.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from typing import Any, FrozenSet, List, Mapping, Optional, Sequence, Tuple

TEST_RUNNER_TYPE = "coordinator:test-runner"

LEG_IDENTITY = "identity"
LEG_SCOPE = "scope"
LEG_CEILING = "ceiling"

_REMEDY = "dispatch coordinator:test-runner with the touched test node ids"
_ADVISORY_PREFIX = "test-run-admission advisory: "


@dataclasses.dataclass(frozen=True)
class TestRunVerdict:
    """allowed: may the run start. leg: the refusing leg ("" when allowed). fact/remedy: one fact
    and one terse alternative, empty when allowed."""

    __test__ = False  # pytest must not collect this as a test class

    allowed: bool
    leg: str = ""
    fact: str = ""
    remedy: str = ""


_ALLOW = TestRunVerdict(True)


def _deny(leg: str, fact: str, remedy: str = _REMEDY) -> TestRunVerdict:
    return TestRunVerdict(False, leg, fact, remedy)


def _advisory(message: str) -> None:
    print(_ADVISORY_PREFIX + message, file=sys.stderr)


# --------------------------------------------------------------------------- argv reading

def _pytest_arg_list(argv: Sequence[str]) -> List[str]:
    """Arguments after pytest for a full argv (`pytest ...`, `python -m pytest ...`); an argv that
    runs no pytest head is taken as the arguments themselves."""
    from coordinator_core.bash_guards._heavy_command_class import _pytest_args

    args = _pytest_args(argv)
    return list(args) if args is not None else list(argv)


def _numprocesses(args: Sequence[str]) -> Optional[str]:
    """The last `-n`/`--numprocesses` value given, or None when none is."""
    value: Optional[str] = None
    for j, tok in enumerate(args):
        if tok == "-n" or tok == "--numprocesses":
            value = args[j + 1] if j + 1 < len(args) else ""
        elif tok.startswith("--numprocesses="):
            value = tok.split("=", 1)[1]
        elif tok.startswith("-n") and not tok.startswith("--") and len(tok) > 2:
            value = tok[2:].lstrip("=")
    return value


def _has_workers(args: Sequence[str]) -> bool:
    raw = _numprocesses(args)
    if raw is None:
        return False
    try:
        return int(raw) > 0
    except ValueError:
        return True  # auto, logical, or unparseable: fans out or cannot be shown not to


def _git_root(start: str) -> Optional[str]:
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _key(path: str) -> str:
    return os.path.normcase(path.replace("\\", "/"))


def _repo_relative(file_part: str, cwd: str, repo_root: str) -> str:
    joined = file_part if os.path.isabs(file_part) else os.path.join(cwd, file_part)
    try:
        rel = os.path.relpath(os.path.normpath(joined), repo_root)
    except ValueError:  # another drive
        rel = os.path.normpath(joined)
    return _key(rel)


# --------------------------------------------------------------------------- touch record

def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _read_touched(payload: Mapping[str, Any], repo_root: Optional[str]) -> Optional[Tuple[FrozenSet[str], bool]]:
    """(test files, degraded) from the SESSION hub sink, or None when the record cannot be read.
    The session sink, not the agent's: the test-runner verifies other agents' edits."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id or not repo_root:
        return None
    try:
        from pathlib import Path

        from coordinator_core.lifecycle import git_common_dir
        from coordinator_core.session import touch_record
        from coordinator_core.session_hub_path import session_dir_for

        sink = touch_record.sink_path(session_dir_for(Path(git_common_dir(Path(repo_root))), session_id))
        projection = touch_record.project_live_claims(sink, cwd=repo_root)
    except Exception:  # noqa: BLE001 -- an unreadable record allows and says so
        return None
    tests = frozenset(_key(p) for p in projection.claims if _is_test_file(p.replace("\\", "/")))
    return tests, bool(projection.degraded)


def touched_test_files(payload: Mapping[str, Any], repo_root: Optional[str]) -> FrozenSet[str]:
    """Repo-relative test files the payload's session touched, or an empty set when the record is
    unreadable. Paths are normalised with `os.path.normcase` after `/` folding."""
    read = _read_touched(payload, repo_root)
    return read[0] if read is not None else frozenset()


# --------------------------------------------------------------------------- ceiling

def over_admit_ceiling(payload: Mapping[str, Any]) -> Optional[str]:
    """A fact string when the box is below heavy_admission's RAM floor or held by another session,
    else None. An unreadable or untrusted reading is None plus an advisory."""
    from coordinator_core.bash_guards import guard_heavy_command_admission as heavy
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_FREE_RAM_FLOOR_MB

    try:
        hold = heavy._read_box_hold()
    except Exception as exc:  # noqa: BLE001
        _advisory("box-hold record unreadable (%s)" % type(exc).__name__)
        hold = None
    if hold is not None and hold.set_by_session != payload.get("session_id"):
        return "the box is held by %s: %s" % (hold.set_by_session, hold.reason)
    try:
        floor = heavy._positive_int(heavy._read_config(), KEY_FREE_RAM_FLOOR_MB)
        reading = heavy._read_available()
    except Exception as exc:  # noqa: BLE001
        _advisory("RAM reading unavailable (%s)" % type(exc).__name__)
        return None
    if floor is None:
        _advisory("heavy_admission free_ram_floor_mb unconfigured")
        return None
    if not reading.trusted or reading.avail_mb is None:
        _advisory("available RAM untrusted (%s)" % reading.source)
        return None
    if reading.avail_mb < floor:
        return "%d MB available, below the %d MB floor" % (reading.avail_mb, floor)
    return None


# --------------------------------------------------------------------------- verdict

def _scope_verdict(payload: Mapping[str, Any], args: Sequence[str], cwd: str) -> TestRunVerdict:
    from coordinator_core.bash_guards._heavy_command_class import _pytest_targets

    if _has_workers(args):
        return _deny(LEG_SCOPE, "the test-runner runs serially: -n workers refused")
    targets = _pytest_targets(args)
    if not targets:
        return _deny(LEG_SCOPE, "the test-runner names touched test files or node ids: a bare run refused")
    files = [t.split("::", 1)[0] for t in targets]
    bad = [t for t, f in zip(targets, files) if not f.lower().endswith(".py")]
    if bad:
        return _deny(LEG_SCOPE, "the test-runner names test files, not %s" % bad[0])
    repo_root = _git_root(cwd)
    read = _read_touched(payload, repo_root)
    if read is None:
        _advisory("touch record unreadable; scope leg allows")
        return _ALLOW
    touched, degraded = read
    for target, file_part in zip(targets, files):
        assert repo_root is not None
        if _repo_relative(file_part, cwd, repo_root) not in touched:
            if degraded:
                _advisory("touch record degraded; %s not found, scope leg allows" % target)
                continue
            # Advisory, never a deny: a test run is recoverable, and the touch record misses legitimate
            # targets (a file a subagent wrote and committed is in no session sink).
            _advisory("%s is not in this session's touched test files; scope leg allows" % target)
    return _ALLOW


def test_run_verdict(
    payload: Mapping[str, Any],
    pytest_argvs: Sequence[Sequence[str]],
    cwd: str,
    *,
    is_subagent: bool,
) -> TestRunVerdict:
    """May this caller start these pytest invocations now. `pytest_argvs` is one argv per pytest
    invocation in the command, full (`python -m pytest a.py`) or already stripped to pytest's own
    arguments; none means no test run and allows. Never raises, never spawns."""
    if not pytest_argvs:
        return _ALLOW
    if is_subagent:
        if payload.get("agent_type") != TEST_RUNNER_TYPE:
            return _deny(LEG_IDENTITY, "only coordinator:test-runner runs pytest in a subagent")
        for argv in pytest_argvs:
            verdict = _scope_verdict(payload, _pytest_arg_list(argv), cwd)
            if not verdict.allowed:
                return verdict
    fact = over_admit_ceiling(payload)
    if fact is not None:
        return _deny(LEG_CEILING, fact, "retry once the box recovers or the hold clears")
    return _ALLOW
