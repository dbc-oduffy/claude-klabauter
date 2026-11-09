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
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.git.run import run_git
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.test_interpreter import resolve_targeted_test_python
from coordinator_core.resolve_validation_cmd import read_local_md_key
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
    """`verdict` is "green" (tests ran and passed), "red" (a test failed) or "inconclusive" (no overlay or
    no test ran: never a refusal). `detail` is the tail of the runner output or the reason."""

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


def _run(argv: list, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, errors="replace", timeout=timeout,
        **no_console_creationflags(),
    )


def _gate_python(root: Path) -> str:
    """The interpreter the repo's test gate pins (same ladder as `retest_targeted`), read from the live
    repo's coordinator.local.md; `sys.executable` when none is pinned or the pin does not resolve."""
    pin = resolve_targeted_test_python(root)
    return pin.command if pin.command and pin.defect is None else sys.executable


def verify(root: Path, run_id: str, candidate_sha: str, seeds: Sequence[str]) -> OverlayVerdict:
    """Clone `root` shared into the run's overlay dir, check out `candidate_sha` detached, run pytest over
    `seeds` there, and reap the overlay on every exit path. Never raises: a setup failure, a timeout or a
    run that collected nothing is "inconclusive"."""
    seeds = tuple(seeds)
    if not seeds:
        return OverlayVerdict("inconclusive", "the landing set has no test seeds", seeds)
    holder = overlay_root(root, run_id)
    dest = holder / root.resolve().name
    try:
        shutil.rmtree(holder, ignore_errors=True)
        holder.mkdir(parents=True, exist_ok=True)
        clone = run_git(["clone", "--shared", "--no-checkout", "-q", str(root), str(dest)], cwd=str(root))
        if clone.returncode:
            return OverlayVerdict("inconclusive", f"overlay clone failed: {clone.stderr.strip()[-_OUTPUT_TAIL:]}", seeds)
        out = run_git(["checkout", "--detach", "-q", candidate_sha], cwd=str(dest))
        if out.returncode:
            return OverlayVerdict("inconclusive", f"overlay checkout failed: {out.stderr.strip()[-_OUTPUT_TAIL:]}", seeds)
        ran = _run([_gate_python(root), "-m", "pytest", "-q", "-p", "no:cacheprovider", *seeds], dest, TEST_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return OverlayVerdict("inconclusive", f"overlay verify did not complete: {type(exc).__name__}: {exc}", seeds)
    finally:
        shutil.rmtree(holder, ignore_errors=True)
    tail = (ran.stdout + ran.stderr).strip()[-_OUTPUT_TAIL:]
    if ran.returncode == 0:
        return OverlayVerdict("green", tail, seeds)
    if ran.returncode == _PYTEST_FAILED:
        return OverlayVerdict("red", tail, seeds)
    return OverlayVerdict("inconclusive", f"pytest exit {ran.returncode}: {tail}", seeds)


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
    )
    return path


def check_verdict(root: Path, run_id: str, measured: dict) -> OverlayVerdict:
    """Read the run's overlay verdict for the `measured` record. One file read, no spawn: "green" or "red"
    (or "inconclusive", which land_run lands with an advisory) when a verdict keyed to exactly this landing
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
    if verdict not in ("green", "red"):
        return OverlayVerdict("missing", f"overlay verdict was {verdict!r}: {data.get('detail', '')}", seeds)
    return OverlayVerdict(verdict, str(data.get("detail", "")), seeds)


def remedy_call(run_id: Optional[str]) -> str:
    return f"{Path(sys.executable).name} -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id {run_id or '<run_id>'} --repo-root <repo_root>"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Verify-stage entry: read the run's `measured-rev.json`, run the overlay over the landing set's test
    seeds and write the verdict. Exit 0 on green, 1 on red, 2 when inconclusive or unmeasured."""
    from coordinator_core.ops.dispatch_emit.test_reach import seeds_for_landing

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
    sha = measured.get("sha")
    if not isinstance(sha, str):
        print("measured-rev record carries no candidate sha", file=sys.stderr)
        return 2
    outcome = verify(root, args.run_id, sha, seeds_for_landing(root, measured.get("paths") or []))
    write_verdict(root, args.run_id, measured, outcome)
    print(f"overlay verdict: {outcome.verdict}")
    return {"green": 0, "red": 1}.get(outcome.verdict, 2)


if __name__ == "__main__":
    raise SystemExit(main())
