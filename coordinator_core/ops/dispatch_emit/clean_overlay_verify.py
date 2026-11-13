"""Clean-overlay verify: run a landing set's scoped tests in a `git clone --shared` of the measured
candidate commit (HEAD plus only the landing set), so a peer's dirty tracked file can neither fail nor
falsely green the verdict.

Runs in the workflow verify stage (`python -m coordinator_core.ops.dispatch_emit.clean_overlay_verify
--run-id <run>`), never inside an engine op: it writes `overlay-verdict.json` beside the run's
`measured-rev.json`, keyed to the landing set's blob shas and the base sha. `dispatch.land_run` only
calls `check_verdict`, a single file read."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.git.run import _git_exe, run_git
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.interpreter_pin import resolve_targeted_test_python
from coordinator_core.resolve_validation_cmd import read_local_md_key
from coordinator_core.testing import scoped_pytest
from coordinator_core.win_portability import no_console_creationflags

VERDICT_FILE = "overlay-verdict.json"

OVERLAY_DIR = ("scratch", "clean-overlay")
BULK_DIR_KEY = "bulk_scratch_dir"
SETUP_TIMEOUT_S = 60
TEST_TIMEOUT_S = 120
_PYTEST_FAILED = 1
_OUTPUT_TAIL = 2000


@dataclass(frozen=True)
class OverlayVerdict:
    """`verdict` is "green" (tests ran and passed), "red" (a test failed), "pre_existing" (every failing test
    also fails at the base sha: lands with an advisory) or "inconclusive" (no overlay or no test ran: never a
    refusal). `detail` is the tail of the runner output or the reason."""

    verdict: str
    detail: str = ""
    seeds: tuple = field(default_factory=tuple)

    @property
    def red(self) -> bool:
        return self.verdict == "red"


def overlay_root(root: Path, run_id: str) -> Path:
    """The run's holder dir: `<bulk_scratch_dir>/clean-overlay/<run>` when the repo's coordinator.local.md
    declares an absolute `bulk_scratch_dir:`, else `<root>/scratch/clean-overlay/<run>`. One config read, no spawn.
    The clone lives one level down, named as `root`'s own directory: tests derive the repo identity (and its
    per-repo exemptions) from the clone's basename, so a run-id name would drop them."""
    bulk = read_local_md_key(str(root), BULK_DIR_KEY).strip().strip("\"'")
    if bulk and Path(bulk).is_absolute():
        return Path(bulk).joinpath(*OVERLAY_DIR[1:], run_id)
    return root.joinpath(*OVERLAY_DIR, run_id)


def _ledger_holder(root: Path, run_id: str, holder: Path) -> None:
    """Record an in-repo holder dir in the run's scratch ledger so a crash leftover is reaped at run end.
    The absolute `bulk_scratch_dir` holder is outside the repo and not ledgered. Never raises."""
    try:
        from coordinator_core.ops.dispatch_emit import run_scratch

        if holder == root.joinpath(*OVERLAY_DIR, run_id):
            run_scratch.record_paths(root, run_id, ["/".join((*OVERLAY_DIR, run_id))])
    except (OSError, ValueError):
        pass


def _run(argv: list, cwd: Path, timeout: int, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run `argv` in a capped job (a killpg session off Windows); the tree is closed on every exit, so a
    timed-out pytest leaves no grandchild. Raises `subprocess.TimeoutExpired` at the timeout."""
    from coordinator_core.diagnostics.contained_run import spawn_in_capped_job

    proc, close = spawn_in_capped_job(
        argv,
        popen_kwargs=dict(
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", **no_console_creationflags(),
        ),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        close()
        proc.communicate()
        raise
    finally:
        close()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _gate_python(root: Path) -> str:
    """The interpreter the repo's test gate pins (same ladder as `retest_targeted`), read from the live
    repo's coordinator.local.md; `sys.executable` when none is pinned or the pin does not resolve."""
    pin = resolve_targeted_test_python(root)
    return pin.command if pin.command and pin.defect is None else sys.executable


def engine_python_command(root: Path) -> str:
    """`_gate_python(root)` as a token safe to paste into a Bash command: an absolute path is posix-slashed
    and double-quoted when it holds whitespace; a pinned bare name or launcher clause is returned as declared.
    Never a bare `python`: an agent's PATH `python` can lack `coordinator_core`."""
    py = _gate_python(root)
    if Path(py).is_absolute():
        py = Path(py).as_posix()
        return f'"{py}"' if any(c.isspace() for c in py) else py
    return py


_PASSED_RE = re.compile(r"\b\d+ passed\b")
_NOT_PASSED_RE = re.compile(r"\b\d+ (?:failed|errors?)\b|^(?:FAILED|ERROR) ", re.MULTILINE)
_TEARDOWN_MARKERS = ("pytest_sessionfinish", "pytest_unconfigure", "cleanup_dead_symlinks", "pytest_terminal_summary")
_PYTEST_INTERNAL_ERROR = 3


def teardown_only_error(returncode: int, output: str) -> bool:
    """True when a non-zero pytest exit is only a session-teardown error: the run reports passed tests, no
    failed or errored test, and an INTERNALERROR (or a failure exit) whose trace sits in a sessionfinish-time
    hook. A failed test, a collection error or an INTERNALERROR with no teardown frame is not."""
    if returncode not in (_PYTEST_FAILED, _PYTEST_INTERNAL_ERROR) or not _PASSED_RE.search(output):
        return False
    if _NOT_PASSED_RE.search(output) or "INTERNALERROR" not in output:
        return False
    return any(marker in output for marker in _TEARDOWN_MARKERS)


_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - |$)", re.MULTILINE)


def failing_ids(output: str) -> tuple:
    """The node ids a pytest short summary reports as FAILED or ERROR, in order, without duplicates."""
    return tuple(dict.fromkeys(m.strip() for m in _FAILED_ID_RE.findall(output)))


def _base_failing(dest: Path, root: Path, run_id: str, base_sha: str, ids: Sequence[str]) -> frozenset:
    """The subset of `ids` that also fail with `base_sha` checked out in the overlay clone `dest`. Any
    setup problem or timeout yields the empty set: an unmeasured base never excuses a red."""
    run_holder = None
    try:
        out = _run([_git_exe(), "checkout", "--detach", "-q", base_sha], dest, TEST_TIMEOUT_S)
        if out.returncode:
            return frozenset()
        run_holder, basetemp, tmp = scoped_pytest.per_run_dirs(root, f"{run_id}-base")
        ran = _run(
            [_gate_python(root), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(basetemp), *ids],
            dest, TEST_TIMEOUT_S, env=scoped_pytest.tmp_env(os.environ, tmp),
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    finally:
        if run_holder is not None:
            shutil.rmtree(run_holder, ignore_errors=True)
    return frozenset(failing_ids(ran.stdout + ran.stderr)) & frozenset(ids)


def verify(
    root: Path, run_id: str, candidate_sha: str, seeds: Sequence[str], base_sha: Optional[str] = None,
) -> OverlayVerdict:
    """Clone `root` shared into the run's overlay dir, check out `candidate_sha` detached, run pytest over
    `seeds` there, and reap the overlay on every exit path. A red whose every failing node id also fails at
    `base_sha` (re-run in the same clone, those ids only) is "pre_existing": the run did not cause it.
    Never raises: a setup failure, a timeout or a run that collected nothing is "inconclusive"."""
    seeds = tuple(seeds)
    if not seeds:
        return OverlayVerdict("inconclusive", "the landing set has no test seeds", seeds)
    holder = overlay_root(root, run_id)
    dest = holder / root.resolve().name
    run_dirs: list = []
    _ledger_holder(root, run_id, holder)
    try:
        shutil.rmtree(holder, ignore_errors=True)
        holder.mkdir(parents=True, exist_ok=True)
        clone = run_git(["clone", "--shared", "--no-checkout", "-q", str(root), str(dest)], cwd=str(root))
        if clone.returncode:
            why = "timed out" if clone.timed_out else clone.stderr.strip()[-_OUTPUT_TAIL:]
            return OverlayVerdict("inconclusive", f"overlay clone failed: {why}", seeds)
        # A full-tree checkout is bulk transfer, not local plumbing: under run_git's 2s budget it timed out
        # on this repo and came back rc -1 with empty stderr. It runs under the overlay's pytest bound.
        out = _run([_git_exe(), "checkout", "--detach", "-q", candidate_sha], dest, TEST_TIMEOUT_S)
        if out.returncode:
            return OverlayVerdict("inconclusive", f"overlay checkout failed: {out.stderr.strip()[-_OUTPUT_TAIL:]}", seeds)
        run_holder, basetemp, tmp = scoped_pytest.per_run_dirs(root, run_id)
        run_dirs.append(run_holder)
        ran = _run(
            [_gate_python(root), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(basetemp), *seeds],
            dest, TEST_TIMEOUT_S, env=scoped_pytest.tmp_env(os.environ, tmp),
        )
        full = ran.stdout + ran.stderr
        tail = full.strip()[-_OUTPUT_TAIL:]
        at_base: frozenset = frozenset()
        ids = failing_ids(full)
        if (
            ran.returncode == _PYTEST_FAILED and ids and isinstance(base_sha, str) and base_sha
            and base_sha != candidate_sha and not teardown_only_error(ran.returncode, full)
        ):
            at_base = _base_failing(dest, root, run_id, base_sha, ids)
    except (OSError, subprocess.SubprocessError) as exc:
        return OverlayVerdict("inconclusive", f"overlay verify did not complete: {type(exc).__name__}: {exc}", seeds)
    finally:
        shutil.rmtree(holder, ignore_errors=True)
        for run_dir in run_dirs:
            shutil.rmtree(run_dir, ignore_errors=True)
    if ran.returncode == 0:
        return OverlayVerdict("green", tail, seeds)
    if teardown_only_error(ran.returncode, full):
        return OverlayVerdict("green", f"pytest teardown error after every test passed (exit {ran.returncode}): {tail}", seeds)
    if ran.returncode == _PYTEST_FAILED:
        if ids and at_base == frozenset(ids):
            return OverlayVerdict(
                "pre_existing", f"every failing test also fails at base {str(base_sha)[:12]}: {', '.join(ids)}: {tail}", seeds,
            )
        return OverlayVerdict("red", tail, seeds)
    return OverlayVerdict("inconclusive", f"pytest exit {ran.returncode}: {tail}", seeds)


def overlay_candidate(measured: dict) -> Optional[str]:
    """The commit the overlay checks out: `overlay_sha` (declared paths carrying a peer's hunks split down to
    the run's own) when `measured_rev` recorded one, else the whole-file candidate `sha`."""
    sha = measured.get("overlay_sha") or measured.get("sha")
    return sha if isinstance(sha, str) else None


def name_peer_owners(outcome: OverlayVerdict, measured: dict) -> OverlayVerdict:
    """`outcome` with the peer hunks of the measured record named. A path whose peer hunks were split out is
    noted on any verdict; a path whose peer hunks could not be split turns a red into "inconclusive" (the
    red may be the peer's, so it is never a caused red) naming the owning run."""
    peers = measured.get("peer_hunks")
    if not isinstance(peers, dict) or not peers:
        return outcome
    kept = {p: v for p, v in peers.items() if isinstance(v, dict) and v.get("split")}
    left = {p: v for p, v in peers.items() if isinstance(v, dict) and not v.get("split")}
    def _named(group: dict) -> str:
        return "; ".join(f"{p} (run {v.get('owner')}, lines {v.get('lines')})" for p, v in sorted(group.items()))
    notes = []
    if kept:
        notes.append(f"peer hunks split out of the overlay candidate: {_named(kept)}")
    verdict = outcome.verdict
    if left:
        notes.append(f"peer hunks that could not be split out stay in the candidate: {_named(left)}")
        if outcome.red:
            verdict = "inconclusive"
            notes.append("the red may belong to that run's hunks, not this run: re-run once the owner lands or releases them")
    return OverlayVerdict(verdict, f"{'; '.join(notes)}: {outcome.detail}", outcome.seeds)


def landing_key(measured: dict) -> str:
    """Digest of what the overlay verified: the landing set's blob shas plus the base (HEAD) sha and the
    candidate sha, as recorded by `measured_rev`. A change to any of them changes the key."""
    blobs = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
    payload = json.dumps(
        {"blobs": blobs, "base_sha": measured.get("base_sha"), "sha": measured.get("sha")}, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verdict_path(root: Path, run_id: str) -> Path:
    return measured_rev_record_path(root, run_id).with_name(VERDICT_FILE)


def write_verdict(root: Path, run_id: str, measured: dict, outcome: OverlayVerdict) -> Path:
    path = verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "verdict": outcome.verdict, "detail": outcome.detail, "seeds": list(outcome.seeds),
                "key": landing_key(measured), "base_sha": measured.get("base_sha"), "sha": measured.get("sha"),
            },
            indent=2, sort_keys=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def check_verdict(root: Path, run_id: str, measured: dict) -> OverlayVerdict:
    """Read the run's overlay verdict for the `measured` record. One file read, no spawn: "green" or "red"
    (or "inconclusive" / "pre_existing", which land_run lands with an advisory) when a verdict keyed to exactly this landing
    set and base sha exists; otherwise "missing" (no file) or
    "mismatch" (keyed to a different landing set or base sha; `detail` says which side moved)."""
    try:
        data = json.loads(verdict_path(root, run_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return OverlayVerdict("missing", "no overlay verdict was written for this run")
    except (OSError, ValueError) as exc:
        return OverlayVerdict("missing", f"overlay verdict unreadable: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        return OverlayVerdict("missing", "overlay verdict unreadable: not an object")
    seeds = tuple(data.get("seeds") or ())
    if data.get("key") != landing_key(measured):
        moved = "base sha" if data.get("base_sha") != measured.get("base_sha") else "landing set blobs"
        return OverlayVerdict("mismatch", f"the overlay verdict was keyed to a different {moved}", seeds)
    verdict = data.get("verdict")
    if verdict == "inconclusive":
        return OverlayVerdict("inconclusive", str(data.get("detail", "")), seeds)
    if verdict not in ("green", "red", "pre_existing"):
        return OverlayVerdict("missing", f"overlay verdict was {verdict!r}: {data.get('detail', '')}", seeds)
    return OverlayVerdict(verdict, str(data.get("detail", "")), seeds)


REVERIFY_WAIT_S = SETUP_TIMEOUT_S + TEST_TIMEOUT_S
_REVERIFY_POLL_S = 1.0


def reverify_on_mismatch(
    root: Path, run_id: str, measured: dict, stale: OverlayVerdict, *,
    spawn=None, wait_s: float = REVERIFY_WAIT_S, sleep=time.sleep, clock=time.monotonic,
) -> OverlayVerdict:
    """Re-run the overlay for a "mismatch" `stale` verdict over the current `measured` candidate: this
    module's CLI is launched detached (the overlay's git and pytest children never run inside the engine
    op) and its verdict file is polled for at most `wait_s`. Returns the fresh verdict when it is green or
    red; any other `stale`, no recorded seeds, no candidate, a failed launch, a timeout or an inconclusive
    re-verify returns `stale` unchanged (the child still writes its verdict for the refire)."""
    if stale.verdict != "mismatch" or not stale.seeds or overlay_candidate(measured) is None:
        return stale
    if spawn is None:
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached as spawn
    engine = Path(__file__).resolve().parents[3]
    argv = ["--run-id", run_id, "--repo-root", str(root)]
    if not spawn(str(engine), str(Path(__file__).resolve()), argv):
        return stale
    deadline = clock() + wait_s
    while True:
        sleep(_REVERIFY_POLL_S)
        fresh = check_verdict(root, run_id, measured)
        if fresh.verdict not in ("mismatch", "missing"):
            return fresh if fresh.verdict in ("green", "red", "pre_existing") else stale
        if clock() >= deadline:
            return stale


def remedy_call(run_id: Optional[str], root: Optional[Path] = None) -> str:
    py = engine_python_command(root) if root is not None else Path(sys.executable).as_posix()
    return f"{py} -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id {run_id or '<run_id>'} --repo-root <repo_root>"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Verify-stage entry: read the run's `measured-rev.json`, run the overlay over the landing set's test
    seeds and write the verdict. Exit 0 on green, 1 on red, 2 when inconclusive or unmeasured."""
    from coordinator_core.ops.dispatch_emit.landing_reach import seeds_for_landing

    parser = argparse.ArgumentParser(prog="clean_overlay_verify")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    if not safe_id(args.run_id):
        parser.error(f"--run-id {args.run_id!r} is not a plain run id")
    root = Path(args.repo_root).resolve()
    try:
        measured = json.loads(measured_rev_record_path(root, args.run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"no readable measured-rev record for {args.run_id}: {exc}", file=sys.stderr)
        return 2
    sha = overlay_candidate(measured)
    if sha is None:
        print("measured-rev record carries no candidate sha", file=sys.stderr)
        return 2
    outcome = name_peer_owners(
        verify(
            root, args.run_id, sha, seeds_for_landing(root, measured.get("paths") or []),
            base_sha=measured.get("base_sha"),
        ), measured,
    )
    write_verdict(root, args.run_id, measured, outcome)
    print(f"overlay verdict: {outcome.verdict}")
    return {"green": 0, "pre_existing": 0, "red": 1}.get(outcome.verdict, 2)


if __name__ == "__main__":
    raise SystemExit(main())
