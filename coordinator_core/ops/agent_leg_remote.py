"""agent-leg-remote: run an executor or review agent leg on the least-loaded fleet box (HEAD plus the run delta), else report `placed: false` so the caller runs its in-harness agent.

Usage: python -m coordinator_core.ops.agent_leg_remote --leg executor|review --prompt-file <path> --out <run_dir>/<leg>.result.json
           [--repo-root <path>] [--run-id <id>] [--model <name>] [--schema-name <name> | --schema-file <path>]
           [--budget-s N] [--writes <path> ...]

The far side is the allowlisted `agent_leg.remote_run` / `agent_leg.remote_poll` ops (`remote_run` / `remote_poll`
here); transport, bundle and receipt shape are `test_leg_remote`'s. Stdout and `--out` carry
`{placed, leg, box_id, reply, fallback, reason}`; the receipt is `<out stem>-placement.json` beside `--out`.
An executor's far-tree binary diff is applied locally to `--writes` only; an executor with no declared write
is never placed. Exit 0 whether or not the leg was placed; 2 is a usage error.

Trap: the far executor holds `Bash` in an export of HEAD plus the declared delta, so it can write any path in that
export; the local apply keeps only `--writes` paths and reports the rest as `dropped_paths`.
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
from typing import Callable, NoReturn, Optional, Sequence

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import box_transport, leg_placement, run_delta
from coordinator_core.ops import test_leg_remote as tlr
from coordinator_core.ops import test_leg_run
from coordinator_core.win_portability import no_console_creationflags

LEGS = ("executor", "review")
DEFAULT_BUDGET_S = 3600.0
POLL_INTERVAL_S = 20.0
REPLY_TAIL_CHARS = 8000
EXIT_USAGE = 2
CALL_RUN = "agent_leg.remote_run"
CALL_POLL = "agent_leg.remote_poll"
_TOOLS = {
    "executor": ("acceptEdits", "Read,Edit,Write,Glob,Grep,Bash"),
    "review": ("default", "Read,Glob,Grep,Bash(git:*)"),
}
_DIFF_HEADER = re.compile(rb"^diff --git a/(.+?) b/(.+)$", re.M)

Remote = Callable[..., dict]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="agent_leg_remote", add_help=False, allow_abbrev=False)
    p.add_argument("--leg", choices=LEGS)
    p.add_argument("--prompt-file")
    p.add_argument("--out")
    p.add_argument("--repo-root")
    p.add_argument("--run-id")
    p.add_argument("--model")
    p.add_argument("--schema-name")
    p.add_argument("--schema-file")
    p.add_argument("--budget-s", type=float)
    p.add_argument("--writes", nargs="*", default=[])
    return p


def _receipt_path(out: Path) -> Path:
    stem = out.name[: -len(".result.json")] if out.name.endswith(".result.json") else out.stem
    return out.parent / f"{stem}-placement.json"


def _schema(args) -> Optional[dict]:
    if args.schema_file:
        return json.loads(Path(args.schema_file).read_text(encoding="utf-8"))
    if args.schema_name:
        from coordinator_core.ops.dispatch_emit import wake_digest

        return json.loads(wake_digest.stage_schema_literal(args.schema_name))
    return None


def _diff_paths(diff: bytes) -> list[str]:
    return list(dict.fromkeys(m.group(2).decode("utf-8", "replace") for m in _DIFF_HEADER.finditer(diff)))


def apply_diff(root: Path, diff: bytes, writes: Sequence[str]) -> tuple[list[str], list[str], str]:
    """`(applied, dropped, error)`: `diff` applied to `root` for the `writes` paths only, atomically; `error` is empty on success."""
    paths = _diff_paths(diff)
    allowed = set(writes)
    keep = [p for p in paths if p in allowed]
    dropped = [p for p in paths if p not in allowed]
    if not keep:
        return [], dropped, ""
    args = ["git", "-C", str(root), "apply", "--binary", "--whitespace=nowarn", *[f"--include={p}" for p in keep], "-"]
    done = subprocess.run(args, input=diff, capture_output=True, **no_console_creationflags())
    if done.returncode != 0:
        return [], dropped, done.stderr.decode("utf-8", "replace").strip()[-300:]
    return keep, dropped, ""


def _remote_leg(box: dict, bundle: dict, root: Path, spec: dict, deadline: float, remote: Remote,
                clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple[Optional[dict], str]:
    """`(result, "")` from the far box, else `(None, reason)`: the caller reports a local fallback."""
    params: dict = {
        **spec, "head_sha": bundle["head_sha"], "digest": bundle["digest"],
        "patch_b64": base64.b64encode(bundle["patch"]).decode(),
        "files_b64": {p: base64.b64encode(b).decode() for p, b in bundle["files"].items()},
    }
    reply, reason = tlr._call(remote, box, CALL_RUN, params, tlr.CALL_TIMEOUT_S)
    if reply is not None and reply.get("ok") is False and reply.get("reason") == "sha_absent":
        packed = tlr._git_bundle(root, reply.get("have"))
        if packed is None:
            return None, "sha_absent_bundle_failed"
        params["git_bundle_b64"] = base64.b64encode(packed).decode()
        reply, reason = tlr._call(remote, box, CALL_RUN, params, tlr.CALL_TIMEOUT_S)
    if reply is None:
        return None, reason
    job = reply.get("job")
    if reply.get("ok") is not True or not isinstance(job, str):
        return None, str(reply.get("reason", "far_refused"))
    failures = 0
    while clock() < deadline:
        sleep(POLL_INTERVAL_S)
        reply, reason = tlr._call(remote, box, CALL_POLL, {"job": job}, tlr.POLL_TIMEOUT_S)
        if reply is None or reply.get("ok") is not True:
            failures += 1
            if failures >= tlr.POLL_FAILURES_MAX:
                return None, f"poll_failed:{reason or (reply or {}).get('reason', 'far_refused')}"
            continue
        failures = 0
        state = reply.get("state")
        if state == "lost":
            return None, "remote_job_lost"
        if state == "done":
            verdict = reply.get("verdict")
            return (verdict, "") if isinstance(verdict, dict) else (None, f"no_result:{reply.get('stage', 'run')}")
    return None, "remote_timeout"


def _emit(out: Path, body: dict) -> None:
    try:
        tlr._write_json(out, body)
    except OSError as exc:
        print(f"agent_leg_remote: result not written: {exc}", file=sys.stderr)
    print(json.dumps(body))


def _receipt(out: Path, leg: str, choice: dict, bundle: Optional[dict], fallback: Optional[str], reason: str,
             dropped: Sequence[str] = ()) -> None:
    body = {
        "leg": leg, "box_id": choice.get("box_id"), "load_pct": choice.get("load_pct"),
        "local_load_pct": choice.get("local_load_pct"), "head_sha": (bundle or {}).get("head_sha"),
        "delta_digest": (bundle or {}).get("digest"), "fallback": fallback, "reason": reason,
        "dropped_paths": list(dropped),
    }
    try:
        tlr._write_json(_receipt_path(out), body)
    except OSError as exc:
        print(f"agent_leg_remote: receipt not written: {exc}", file=sys.stderr)


def main(argv: list[str], *, chooser: Callable[..., dict] = leg_placement.choose_leg_box,
         remote: Remote = box_transport.run_remote_payload, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except ValueError as exc:
        print(f"agent_leg_remote: bad arguments: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not (args.leg and args.prompt_file and args.out):
        print("agent_leg_remote: --leg, --prompt-file and --out are required", file=sys.stderr)
        return EXIT_USAGE
    out, leg = Path(args.out), args.leg
    started = clock()
    budget_s = args.budget_s if args.budget_s is not None else DEFAULT_BUDGET_S
    choice: dict = {"box_id": None, "load_pct": None, "local_load_pct": None}
    bundle: Optional[dict] = None
    result: Optional[dict] = None
    dropped: list[str] = []
    reason = ""
    box = None
    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        schema = _schema(args)
    except (OSError, ValueError) as exc:
        prompt, schema, reason = "", None, f"inputs_unreadable:{type(exc).__name__}"
    writes = [w for w in args.writes if w and run_delta._safe_rel(w)]
    if not reason and leg == "executor" and not writes:
        reason = "no_declared_writes"
    if not reason:
        try:
            choice = chooser(leg)
        except Exception as exc:  # noqa: BLE001 -- placement never fails the leg
            choice = {**choice, "box": None}
            reason = f"chooser_failed:{type(exc).__name__}"
        reason = reason or str(choice.get("reason", ""))
        box = choice.get("box")
    if box is not None:
        root, root_error = test_leg_run._leg_root(args.repo_root)
        try:
            if root is None:
                raise RuntimeError(root_error)
            bundle = run_delta.bundle(root, args.run_id)
        except Exception as exc:  # noqa: BLE001 -- an unbundlable tree runs locally
            reason = f"bundle_failed:{type(exc).__name__}:{exc}"[:300]
        else:
            spec = {"leg": leg, "prompt": prompt, "schema": schema, "model": args.model, "budget_s": budget_s,
                    "writes": writes}
            result, reason = _remote_leg(box, bundle, root, spec, started + budget_s + tlr.GRACE_S, remote, clock, sleep)
        if result is not None and leg == "executor" and root is not None:
            diff = base64.b64decode(result.get("diff_b64") or "")
            _, dropped, error = apply_diff(root, diff, writes)
            if error:
                result, reason = None, f"diff_apply_failed:{error}"
    if result is not None:
        _receipt(out, leg, choice, bundle, None, "", dropped)
        _emit(out, {"placed": True, "leg": leg, "box_id": choice.get("box_id"), "reply": result.get("reply"),
                    "fallback": None, "reason": ""})
        return 0
    _receipt(out, leg, choice, bundle, "local", reason)
    _emit(out, {"placed": False, "leg": leg, "box_id": choice.get("box_id"), "reply": None,
                "fallback": "local", "reason": reason})
    return 0


def _agent_cli() -> Optional[str]:
    """The far box's headless `claude` executable: PATH first, then the per-user install directories a non-interactive ssh session lacks."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for rel in (".local/bin/claude", ".claude/local/claude", ".npm-global/bin/claude", ".local/bin/claude.exe"):
        cand = home / rel
        if cand.is_file():
            return str(cand)
    return None


def remote_run(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Far op `agent_leg.remote_run`: verify the bundle and the agent CLI, then start the detached leg and return `{ok, job}`."""
    try:
        leg, prompt = params["leg"], params["prompt"]
        if leg not in LEGS or not isinstance(prompt, str) or not prompt:
            raise ValueError("bad shape")
        budget_s = float(params["budget_s"])
        head, digest = params["head_sha"], params["digest"]
        patch = base64.b64decode(params["patch_b64"])
        files = {p: base64.b64decode(b) for p, b in params["files_b64"].items()}
        if not tlr._SHA.fullmatch(head):
            raise ValueError("bad sha")
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"ok": False, "reason": "bad_params"}
    if run_delta._digest(head, patch, files) != digest:
        return {"ok": False, "reason": "digest_mismatch"}
    cli = _agent_cli()
    if cli is None:
        return {"ok": False, "reason": "no-agent-cli"}
    scratch = tlr._scratch_root()
    if scratch is None:
        return {"ok": False, "reason": f"settings-key-missing: {run_delta.SCRATCH_KEY}"}
    repo = Path(repo_root) if repo_root else Path(os.getcwd())
    if tlr._git(repo, ["cat-file", "-e", f"{head}^{{commit}}"]).returncode != 0:
        packed = params.get("git_bundle_b64")
        if not packed:
            have = tlr._git(repo, ["rev-parse", "HEAD"]).stdout.decode(errors="replace").strip()
            return {"ok": False, "reason": "sha_absent", "have": have if tlr._SHA.fullmatch(have) else None}
        if tlr._git(repo, ["bundle", "unbundle", "-"], base64.b64decode(packed)).returncode != 0 or \
                tlr._git(repo, ["cat-file", "-e", f"{head}^{{commit}}"]).returncode != 0:
            return {"ok": False, "reason": "bundle_unusable"}
    job = f"{digest[:16]}-{uuid.uuid4().hex[:8]}"
    jdir = tlr._job_dir(scratch, job)
    jdir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(jdir / "job.json", json.dumps({
        "repo": str(repo), "head_sha": head, "digest": digest, "patch_b64": params["patch_b64"],
        "files_b64": params["files_b64"], "leg": leg, "prompt": prompt, "schema": params.get("schema"),
        "model": params.get("model"), "budget_s": budget_s, "cli": cli,
    }).encode("utf-8"))
    pid = tlr._spawn_detached([sys.executable, "-m", "coordinator_core.ops.agent_leg_remote", "--far-wrap", str(jdir)], jdir)
    atomic_write_bytes(jdir / "pid", str(pid).encode("utf-8"))
    return {"ok": True, "job": job}


def remote_poll(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Far op `agent_leg.remote_poll`: `{ok, state: running|lost|done, rc, stage, verdict}` for a job `remote_run` started; `verdict` is `{reply, diff_b64}`."""
    job = params.get("job") if isinstance(params, dict) else None
    scratch = tlr._scratch_root()
    if not isinstance(job, str) or not tlr._JOB.fullmatch(job):
        return {"ok": False, "reason": "bad_params"}
    if scratch is None:
        return {"ok": False, "reason": f"settings-key-missing: {run_delta.SCRATCH_KEY}"}
    jdir = tlr._job_dir(scratch, job)
    if not jdir.is_dir():
        return {"ok": False, "reason": "unknown_job"}
    try:
        done = json.loads((jdir / "exit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = None
    if isinstance(done, dict):
        try:
            verdict = json.loads((jdir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verdict = None
        return {"ok": True, "state": "done", "rc": done.get("rc"), "stage": done.get("stage", "run"),
                "verdict": verdict if isinstance(verdict, dict) else None}
    try:
        pid = int((jdir / "pid").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "state": "running"}
    return {"ok": True, "state": "running" if tlr._alive(pid) else "lost"}


def _reply_of(data: dict, leg: str, schema: object) -> object:
    """The leg's reply out of the `claude -p --output-format json` object: the structured output for a schema leg, else the result text's tail."""
    structured = data.get("structured_output")
    if isinstance(structured, dict):
        return structured
    raw = data.get("result")
    text = raw if isinstance(raw, str) else ""
    if schema:
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return text[-REPLY_TAIL_CHARS:]


def _wrap(jdir: Path) -> int:
    """Detached far-side leg: apply the bundle, baseline-commit the export, run headless `claude -p`, write `result.json` (reply plus the executor's binary diff) and `exit.json`."""
    export: Optional[Path] = None
    stage, rc = "apply", -1
    try:
        spec = json.loads((jdir / "job.json").read_text(encoding="utf-8"))
        bundle = {"head_sha": spec["head_sha"], "digest": spec["digest"], "patch": base64.b64decode(spec["patch_b64"]),
                  "files": {p: base64.b64decode(b) for p, b in spec["files_b64"].items()}}
        script = jdir / "apply.py"
        script.write_text(run_delta.apply_script(bundle), encoding="utf-8")
        applied = subprocess.run([sys.executable, str(script), spec["repo"]], capture_output=True, text=True,
                                 cwd=str(jdir), env=tlr._engine_env(), **no_console_creationflags())
        if applied.returncode != 0:
            raise RuntimeError((applied.stderr or applied.stdout)[-300:])
        export = Path(json.loads(applied.stdout.strip().splitlines()[-1])["export_dir"])
        stage = "checkout"
        ident = ["-c", "user.name=agent-leg", "-c", "user.email=agent-leg@localhost", "-c", "commit.gpgsign=false"]
        for step in (["init", "-q"], ["add", "-A"], [*ident, "commit", "-q", "--no-verify", "-m", "agent-leg"]):
            if tlr._git(export, step).returncode != 0:
                raise RuntimeError(f"git {step[-1]} failed")
        stage = "run"
        leg, schema = spec["leg"], spec.get("schema")
        mode, tools = _TOOLS[leg]
        cmd = [spec["cli"], "-p", "--output-format", "json", "--permission-mode", mode, "--allowedTools", tools]
        if spec.get("model"):
            cmd += ["--model", str(spec["model"])]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        ran = subprocess.run(cmd, input=spec["prompt"].encode("utf-8"), cwd=str(export), capture_output=True,
                             timeout=float(spec["budget_s"]), **no_console_creationflags())
        rc = ran.returncode
        data = json.loads(ran.stdout.decode("utf-8", "replace"))
        if rc != 0 or not isinstance(data, dict) or data.get("is_error"):
            raise RuntimeError(f"claude exited {rc}")
        reply = _reply_of(data, leg, schema)
        if reply is None:
            raise RuntimeError("no structured reply")
        diff = b""
        if leg == "executor":
            stage = "diff"
            tlr._git(export, ["add", "-A"])
            diff = tlr._git(export, ["diff", "--binary", "--cached", "HEAD"]).stdout
        atomic_write_bytes(jdir / "result.json", json.dumps(
            {"reply": reply, "diff_b64": base64.b64encode(diff).decode()}).encode("utf-8"))
        stage = "done"
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
