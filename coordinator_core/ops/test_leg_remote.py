"""test-leg-remote: run a test leg on the least-loaded fleet box (HEAD plus the run delta), else locally, with a placement receipt.

Usage: python -m coordinator_core.ops.test_leg_remote [--budget-s N] [--repo-root <path>] [--run-id <id>] --out <path> -- <ids and test files...>

The argv is test_leg_run's. A call carrying a flag whose meaning needs the run's git history (`--base-sha`,
`--head-sha`, `--base-export-out`, `--touched`, `--measured`, `--measured-tree`) runs locally. The far side is the
allowlisted `test_leg.remote_run` / `test_leg.remote_poll` ops (`remote_run` / `remote_poll` here); the receipt
`<out dir>/test-leg-placement.json` records the box, loads, head sha, delta digest, fallback and reason.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import box_transport, leg_placement, run_delta
from coordinator_core.ipc import register_op
from coordinator_core.ops import test_leg_run
from coordinator_core.win_portability import no_console_creationflags

RECEIPT_NAME = "test-leg-placement.json"
CALL_TIMEOUT_S = 120.0
POLL_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 8.0
POLL_FAILURES_MAX = 3
GRACE_S = 60.0
MIN_LOCAL_BUDGET_S = 30.0
INELIGIBLE_FLAGS = ("--base-sha", "--head-sha", "--base-export-out", "--touched", "--measured", "--measured-tree")
_JOB = re.compile(r"[0-9a-f]{16}-[0-9a-f]{8}")
_SHA = re.compile(r"[0-9a-f]{40}")

Remote = Callable[..., dict]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="test_leg_remote", add_help=False, allow_abbrev=False)
    p.add_argument("--budget-s", type=float)
    p.add_argument("--repo-root")
    p.add_argument("--run-id")
    p.add_argument("--out")
    return p


def _rebudget(argv: Sequence[str], cut: int, remaining: float) -> list[str]:
    pre: list[str] = []
    skip = False
    for tok in argv[:cut]:
        if skip:
            skip = False
        elif tok == "--budget-s":
            skip = True
        elif not tok.startswith("--budget-s="):
            pre.append(tok)
    return [*pre, "--budget-s", f"{remaining:.0f}", *argv[cut:]]


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(body) + "\n").encode("utf-8"))


def _git_bundle(root: Path, have: object) -> Optional[bytes]:
    """`git bundle` of HEAD (minus `have` when the far box's head is known here); None when git cannot make one."""
    args = ["git", "-C", str(root), "bundle", "create", "-", "HEAD"]
    if isinstance(have, str) and _SHA.fullmatch(have):
        known = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{have}^{{commit}}"],
                               capture_output=True, **no_console_creationflags())
        if known.returncode == 0:
            args.append(f"^{have}")
    done = subprocess.run(args, capture_output=True, **no_console_creationflags())
    return done.stdout if done.returncode == 0 and done.stdout else None


def _call(remote: Remote, box: dict, op: str, params: dict, timeout: float) -> tuple[Optional[dict], str]:
    """`(reply, "")` when the transport answered with an object, else `(None, reason)`."""
    res = remote(box, op, json.dumps(params).encode("utf-8"), timeout=timeout)
    if not res.get("ok"):
        return None, str(res.get("reason", "remote_failed"))
    reply = res.get("reply")
    return (reply, "") if isinstance(reply, dict) else (None, "bad_reply")


def _remote_leg(box: dict, bundle: dict, root: Path, targets: list[str], budget_s: float, deadline: float,
                remote: Remote, clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple[Optional[dict], str]:
    """`(verdict, "")` from the far box, else `(None, reason)`: the caller falls back to local."""
    params: dict = {
        "head_sha": bundle["head_sha"], "digest": bundle["digest"], "targets": targets, "budget_s": budget_s,
        "patch_b64": base64.b64encode(bundle["patch"]).decode(),
        "files_b64": {p: base64.b64encode(b).decode() for p, b in bundle["files"].items()},
    }
    reply, reason = _call(remote, box, "test_leg.remote_run", params, CALL_TIMEOUT_S)
    if reply is not None and reply.get("ok") is False and reply.get("reason") == "sha_absent":
        packed = _git_bundle(root, reply.get("have"))
        if packed is None:
            return None, "sha_absent_bundle_failed"
        params["git_bundle_b64"] = base64.b64encode(packed).decode()
        reply, reason = _call(remote, box, "test_leg.remote_run", params, CALL_TIMEOUT_S)
    if reply is None:
        return None, reason
    job = reply.get("job")
    if reply.get("ok") is not True or not isinstance(job, str):
        return None, str(reply.get("reason", "far_refused"))
    failures = 0
    while clock() < deadline:
        sleep(POLL_INTERVAL_S)
        reply, reason = _call(remote, box, "test_leg.remote_poll", {"job": job}, POLL_TIMEOUT_S)
        if reply is None or reply.get("ok") is not True:
            failures += 1
            if failures >= POLL_FAILURES_MAX:
                return None, f"poll_failed:{reason or reply.get('reason', 'far_refused')}"
            continue
        failures = 0
        state = reply.get("state")
        if state == "lost":
            return None, "remote_job_lost"
        if state == "done":
            verdict = reply.get("verdict")
            return (verdict, "") if isinstance(verdict, dict) else (None, f"no_verdict:{reply.get('stage', 'run')}")
    return None, "remote_timeout"


def _receipt(out: Path, choice: dict, bundle: Optional[dict], fallback: Optional[str], reason: str) -> None:
    body = {
        "box_id": choice.get("box_id"), "load_pct": choice.get("load_pct"),
        "local_load_pct": choice.get("local_load_pct"),
        "head_sha": (bundle or {}).get("head_sha"), "delta_digest": (bundle or {}).get("digest"),
        "fallback": fallback, "reason": reason,
    }
    try:
        _write_json(out.parent / RECEIPT_NAME, body)
    except OSError as exc:
        print(f"test_leg_remote: receipt not written: {exc}", file=sys.stderr)


def main(argv: list[str], *, chooser: Callable[..., dict] = leg_placement.choose_leg_box,
         remote: Remote = box_transport.run_remote_payload, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> int:
    argv = list(argv)
    if "--" not in argv:
        return test_leg_run.main(argv)
    cut = argv.index("--")
    targets = argv[cut + 1:]
    try:
        args, unknown = _parser().parse_known_args(argv[:cut])
    except ValueError:
        return test_leg_run.main(argv)
    if not args.out or not targets:
        return test_leg_run.main(argv)
    out = Path(args.out)
    started = clock()
    budget_s = args.budget_s if args.budget_s is not None else test_leg_run.TEST_LEG_MINUTES_BUDGET * 60.0
    blocked = next((f for f in INELIGIBLE_FLAGS if any(t == f or t.startswith(f + "=") for t in argv[:cut])), None)
    choice: dict = {"box_id": None, "load_pct": None, "local_load_pct": None}
    bundle: Optional[dict] = None
    reason = ""
    if blocked or unknown:
        reason = f"remote_ineligible:{blocked or 'unsupported_args'}"
    else:
        try:
            choice = chooser("test")
        except Exception as exc:  # noqa: BLE001 -- placement never fails the leg
            choice = {**choice, "local": True, "box": None}
            reason = f"chooser_failed:{type(exc).__name__}"
        reason = reason or str(choice.get("reason", ""))
    box = None if (blocked or unknown) else choice.get("box")
    verdict: Optional[dict] = None
    if box is not None:
        root, root_error = test_leg_run._leg_root(args.repo_root)
        try:
            if root is None:
                raise RuntimeError(root_error)
            bundle = run_delta.bundle(root, test_leg_run._declared_run(args))
        except Exception as exc:  # noqa: BLE001 -- an unbundlable tree runs locally
            reason = f"bundle_failed:{type(exc).__name__}:{exc}"[:300]
        else:
            verdict, reason = _remote_leg(box, bundle, root, targets, budget_s, started + budget_s + GRACE_S,
                                          remote, clock, sleep)
    if verdict is not None:
        _write_json(out, verdict)
        _receipt(out, choice, bundle, None, "")
        print(json.dumps(verdict))
        return test_leg_run.EXIT_TIMEOUT if verdict.get("over_budget") else test_leg_run.EXIT_OK
    local_argv = argv if box is None else _rebudget(argv, cut, max(budget_s - (clock() - started), MIN_LOCAL_BUDGET_S))
    rc = test_leg_run.main(local_argv)
    _receipt(out, choice, bundle, "local", reason)
    return rc


def _git(repo: Path, args: Sequence[str], data: Optional[bytes] = None) -> "subprocess.CompletedProcess":
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, **no_console_creationflags())


def _scratch_root() -> Optional[Path]:
    from coordinator_core import machine_resolver

    raw = machine_resolver.registry_get(run_delta.SCRATCH_KEY)
    return Path(raw) if raw else None


def _job_dir(scratch: Path, job: str) -> Path:
    return scratch / f"leg-{job}"


def remote_run(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Far op `test_leg.remote_run`: verify the delta bundle, then start the detached leg and return `{ok, job}`."""
    try:
        head, digest = params["head_sha"], params["digest"]
        patch = base64.b64decode(params["patch_b64"])
        files = {p: base64.b64decode(b) for p, b in params["files_b64"].items()}
        targets, budget_s = list(params["targets"]), float(params["budget_s"])
        if not _SHA.fullmatch(head) or not targets or not all(isinstance(t, str) for t in targets):
            raise ValueError("bad shape")
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"ok": False, "reason": "bad_params"}
    if run_delta._digest(head, patch, files) != digest:
        return {"ok": False, "reason": "digest_mismatch"}
    scratch = _scratch_root()
    if scratch is None:
        return {"ok": False, "reason": f"settings-key-missing: {run_delta.SCRATCH_KEY}"}
    repo = Path(repo_root) if repo_root else Path(os.getcwd())
    if _git(repo, ["cat-file", "-e", f"{head}^{{commit}}"]).returncode != 0:
        packed = params.get("git_bundle_b64")
        if not packed:
            have = _git(repo, ["rev-parse", "HEAD"]).stdout.decode(errors="replace").strip()
            return {"ok": False, "reason": "sha_absent", "have": have if _SHA.fullmatch(have) else None}
        if _git(repo, ["bundle", "unbundle", "-"], base64.b64decode(packed)).returncode != 0 or \
                _git(repo, ["cat-file", "-e", f"{head}^{{commit}}"]).returncode != 0:
            return {"ok": False, "reason": "bundle_unusable"}
    job = f"{digest[:16]}-{uuid.uuid4().hex[:8]}"
    jdir = _job_dir(scratch, job)
    jdir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(jdir / "job.json", json.dumps({
        "repo": str(repo), "head_sha": head, "digest": digest, "patch_b64": params["patch_b64"],
        "files_b64": params["files_b64"], "targets": targets, "budget_s": budget_s,
    }).encode("utf-8"))
    pid = _spawn_detached([sys.executable, "-m", "coordinator_core.ops.test_leg_remote", "--far-wrap", str(jdir)], jdir)
    atomic_write_bytes(jdir / "pid", str(pid).encode("utf-8"))
    return {"ok": True, "job": job}


def _spawn_detached(argv: list, jdir: Path) -> int:
    """Start `argv` outliving this process (cwd `jdir`, output to `jdir/wrap.log`); its pid."""
    extra = {"start_new_session": True} if os.name != "nt" else {}
    with open(jdir / "wrap.log", "wb") as log:
        proc = subprocess.Popen(argv, cwd=str(jdir), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                env=_engine_env(), **extra, **no_console_creationflags())
    return proc.pid


def _engine_env() -> dict:
    root = str(Path(test_leg_run.__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _alive_windows(pid: int) -> bool:
    """trap: os.kill(pid, 0) terminates the process on Windows; ask the kernel for the exit code instead."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return True


def _alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        if os.name != "posix":
            return _alive_windows(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    except Exception:  # noqa: BLE001
        return True


def remote_poll(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Far op `test_leg.remote_poll`: `{ok, state: running|lost|done, rc, stage, verdict}` for a job `remote_run` started."""
    job = params.get("job") if isinstance(params, dict) else None
    scratch = _scratch_root()
    if not isinstance(job, str) or not _JOB.fullmatch(job):
        return {"ok": False, "reason": "bad_params"}
    if scratch is None:
        return {"ok": False, "reason": f"settings-key-missing: {run_delta.SCRATCH_KEY}"}
    jdir = _job_dir(scratch, job)
    if not jdir.is_dir():
        return {"ok": False, "reason": "unknown_job"}
    try:
        done = json.loads((jdir / "exit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = None
    if isinstance(done, dict):
        try:
            verdict = json.loads((jdir / "tests.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verdict = None
        return {"ok": True, "state": "done", "rc": done.get("rc"), "stage": done.get("stage", "run"),
                "verdict": verdict if isinstance(verdict, dict) else None}
    try:
        pid = int((jdir / "pid").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "state": "running"}
    return {"ok": True, "state": "running" if _alive(pid) else "lost"}


def _agent_remote_run(params: dict, repo_root: Optional[Path] = None) -> dict:
    from coordinator_core.ops import agent_leg_remote

    return agent_leg_remote.remote_run(params, repo_root)


def _agent_remote_poll(params: dict, repo_root: Optional[Path] = None) -> dict:
    from coordinator_core.ops import agent_leg_remote

    return agent_leg_remote.remote_poll(params, repo_root)


register_op("test_leg.remote_run", remote_run)
register_op("test_leg.remote_poll", remote_poll)
register_op("agent_leg.remote_run", _agent_remote_run)
register_op("agent_leg.remote_poll", _agent_remote_poll)


def _wrap(jdir: Path) -> int:
    """Detached far-side leg: apply the bundle, make the export a git checkout, run `test_leg_run`, write `exit.json`."""
    export: Optional[Path] = None
    stage, rc = "apply", -1
    try:
        spec = json.loads((jdir / "job.json").read_text(encoding="utf-8"))
        bundle = {"head_sha": spec["head_sha"], "digest": spec["digest"], "patch": base64.b64decode(spec["patch_b64"]),
                  "files": {p: base64.b64decode(b) for p, b in spec["files_b64"].items()}}
        script = jdir / "apply.py"
        script.write_text(run_delta.apply_script(bundle), encoding="utf-8")
        applied = subprocess.run([sys.executable, str(script), spec["repo"]], capture_output=True, text=True,
                                 cwd=str(jdir), env=_engine_env(), **no_console_creationflags())
        if applied.returncode != 0:
            raise RuntimeError((applied.stderr or applied.stdout)[-300:])
        export = Path(json.loads(applied.stdout.strip().splitlines()[-1])["export_dir"])
        stage = "checkout"
        ident = ["-c", "user.name=test-leg", "-c", "user.email=test-leg@localhost", "-c", "commit.gpgsign=false"]
        for step in (["init", "-q"], ["add", "-A"], [*ident, "commit", "-q", "--no-verify", "-m", "test-leg"]):
            if _git(export, step).returncode != 0:
                raise RuntimeError(f"git {step[-1]} failed")
        stage = "run"
        leg = subprocess.run([sys.executable, "-m", "coordinator_core.ops.test_leg_run", "--repo-root", str(export),
                              "--out", str(jdir / "tests.json"), "--budget-s", str(spec["budget_s"]), "--",
                              *spec["targets"]], cwd=str(jdir), env=_engine_env(), capture_output=True,
                             **no_console_creationflags())
        rc = leg.returncode
    except Exception as exc:  # noqa: BLE001 -- the poll reports the stage that failed
        (jdir / "wrap.err").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
    finally:
        if export is not None:
            shutil.rmtree(export, ignore_errors=True)
        atomic_write_bytes(jdir / "exit.json", json.dumps({"rc": rc, "stage": stage}).encode("utf-8"))
    return 0


def _entry(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--far-wrap":
        return _wrap(Path(argv[1]))
    return main(argv)


if __name__ == "__main__":
    sys.exit(_entry(sys.argv[1:]))
