"""Engine invoker for a family stage: spawns the vendor CLI and records what it wrote.

`python -m coordinator_core.ops.dispatch_emit.family_invoke --root R --run-id ID --row ROW --family F
--stage S --prompt FILE [--writes P ...]` fingerprints the declared writes, runs the family's vendor CLI
from an argv list (agy and grok through `workflow.run_agent`; codex here, under the scoped sandbox),
fingerprints again, and in a `finally` writes `<stage>.invocation.json` plus the raw stdout/stderr
(`family_invoke_record`), so every exit path leaves the record dir. It prints the family result JSON and
exits 0 only when `family_authored`. The record is the only proof `family_credit` accepts that the CLI
authored a path. Every file it writes goes through `claimed_write`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from coordinator_core.ops.dispatch_emit import family_invoke_record as rec_mod
from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.session.claimed_write import replace_bytes, replace_text

__all__ = ("invoke", "main", "resolve_session_id")

_BACKENDS = ("codex", "grok", "agy")
_UNCHANGED = rec_mod.UNCHANGED_CAUSE
_WIN32_CODEX_WRITE = f"{stage_family.WIN32_CODEX_WRITE_REASON}; route this write stage to another family"
_TIMEOUT_S = 3600
_NO_SESSION = (
    "no parent session id: pass --session-id, set COORDINATOR_SESSION_ID, or launch from a run whose "
    "run-stage.json records its session_id"
)
_ARTIFACT_FILES = ("stdout.jsonl", "stderr.txt", "process.json", "last.txt")
_LINE_CAP = 300
_AUTH_RE = re.compile(r"refresh token|not logged in|unauthori[sz]ed|\b401\b|\blog ?in\b|\bauthentication\b", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _launcher_session(root: Path, run_id: str) -> Optional[str]:
    """The `session_id` the run's own `run-stage.json` records as its launcher; None when absent or unreadable."""
    from coordinator_core.session import record_homes

    try:
        data = json.loads((Path(record_homes.home_dir(str(root), "warp")) / run_id / "run-stage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid.strip() if isinstance(sid, str) and sid.strip() else None


def resolve_session_id(root: Path, run_id: str, explicit: Optional[str] = None) -> Optional[str]:
    """Parent session id: `--session-id`, else `COORDINATOR_SESSION_ID`, else the run's recorded launcher."""
    for cand in (explicit, os.environ.get("COORDINATOR_SESSION_ID")):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return _launcher_session(root, run_id)


def _process_exit(artifact_dir: Path) -> Optional[int]:
    try:
        code = json.loads((artifact_dir / "process.json").read_text(encoding="utf-8")).get("exit_code")
    except (OSError, ValueError, AttributeError):
        return None
    return code if isinstance(code, int) else None


def _agent_runner(prompt: str, *, backend: str, repo_root: Path, artifact_dir: Path, parent_session_id: Optional[str] = None) -> dict:
    """agy/grok: `workflow.run_agent`; the CLI's own exit code comes from its process receipt."""
    from coordinator_core.codex_runner import workflow

    error: Optional[str] = None
    try:
        if not parent_session_id:
            raise workflow.AgentFailed(_NO_SESSION)
        workflow.run_agent(
            prompt, {"label": "family-stage"}, repo_root=repo_root, artifact_dir=artifact_dir, backend=backend,
            parent_session_id=parent_session_id,
        )
    except Exception as exc:  # noqa: BLE001 -- AgentFailed and spawn errors alike become the recorded error
        error = f"{type(exc).__name__}: {exc}"[:600]
    code = _process_exit(artifact_dir)
    try:
        argv = workflow._family_argv(backend, artifact_dir)
    except Exception:  # noqa: BLE001 -- unresolved CLI: the error above already names it
        argv = []
    if code is None or (code == 0 and error):
        code = 1 if error else 0
    return {"argv": argv, "exit_code": code, "error": error}


def _codex_runner(prompt: str, *, repo_root: Path, artifact_dir: Path, parent_session_id: Optional[str] = None) -> dict:
    """codex: `exec` under `--sandbox` with writable roots exactly the worktree and its scratch temp dir."""
    from coordinator_core.codex_runner import workflow
    from coordinator_core.codex_runner.codex_spawn_env import WRITABLE_TEMP_RELPATH, shape_codex_child, writeback_codex_auth
    from coordinator_core.codex_runner.resolve import codex_scoped_stage_argv, preflight_auth, resolve_codex
    from coordinator_core.codex_runner.sandbox_preflight import SandboxSetupRefused
    from coordinator_core.ops.workflow_fire.worker_preamble import with_worker_preamble
    from coordinator_core.warm import sandboxed_spawn

    if not parent_session_id:
        raise workflow.AgentFailed(_NO_SESSION)
    if sandboxed_spawn.caller_is_sandboxed():
        raise workflow.AgentFailed("codex child would spawn nested in the codex sandbox; fire through the warm engine")
    root = Path(repo_root).absolute()
    env = workflow._environment()
    preflight_auth(env=env, requested=False)
    cli = resolve_codex(env=env)
    try:
        shape_codex_child(env, writable_root=root)
    except SandboxSetupRefused as exc:
        raise workflow.AgentFailed(str(exc)) from exc
    argv = codex_scoped_stage_argv(
        cli, [str(root), str(root / WRITABLE_TEMP_RELPATH)], "--ephemeral", "-o", str(artifact_dir / "last.txt"), "-",
    )
    proc = None
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = workflow._run_admitted(
                argv, with_worker_preamble("codex", prompt), env=env, repo_root=root, artifact_dir=artifact_dir,
                backend="codex", options={"label": "family-stage"}, parent_session_id=parent_session_id, timeout_s=_TIMEOUT_S,
                on_spawn=None, stdout=out, stderr=err,
            )
        finally:
            writeback_codex_auth(env)
            for handle, name in ((out, "stdout.jsonl"), (err, "stderr.txt")):
                handle.seek(0)
                replace_bytes(artifact_dir / name, handle.read())
    return {"argv": argv, "exit_code": proc.returncode, "error": None if proc.returncode == 0 else f"codex exited {proc.returncode}"}


def _default_runner(prompt: str, *, backend: str, repo_root: Path, artifact_dir: Path, parent_session_id: Optional[str] = None) -> dict:
    if backend == "codex":
        return _codex_runner(prompt, repo_root=repo_root, artifact_dir=artifact_dir, parent_session_id=parent_session_id)
    return _agent_runner(prompt, backend=backend, repo_root=repo_root, artifact_dir=artifact_dir, parent_session_id=parent_session_id)


def _vendor_model(artifact_dir: Path, backend: str) -> Optional[str]:
    """The model the CLI's own output names, via the same parser the process receipt uses; None when it names none."""
    from coordinator_core.codex_runner import workflow

    try:
        model = workflow._event_provenance(artifact_dir / "stdout.jsonl", backend).get("model")
    except Exception:  # noqa: BLE001 -- an unparseable capture names no model
        return None
    return model.strip() if isinstance(model, str) and model.strip() and model != "unknown" else None


def _capture(src: Path, dst: Path) -> None:
    try:
        data = src.read_bytes()
    except OSError:
        data = b""
    replace_bytes(dst, data)


def _stderr_line(artifact_dir: Path) -> str:
    """The stderr line naming the failure: the last auth-matching line, else the last non-empty line; "" when none."""
    try:
        text = (artifact_dir / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _AUTH_RE.search(ln):
            return ln[:_LINE_CAP]
    return lines[-1][:_LINE_CAP] if lines else ""


def _classify_failure(artifact_dir: Path, error: Optional[str], raised: bool) -> tuple:
    """`(cli_failure, blocked_reason)` for a non-zero exit: kind auth|launch|exit plus the quoted cause line."""
    first = (error or "").strip().splitlines()[0][:_LINE_CAP] if (error or "").strip() else ""
    line = first if raised else (_stderr_line(artifact_dir) or first)
    kind = "auth" if _AUTH_RE.search(line) or _AUTH_RE.search(error or "") else "launch" if raised else "exit"
    reason = error if not error or line in error else f"{error}: {line}"
    return {"kind": kind, "stderr_line": line}, reason


def invoke(
    root: Path, run_id: str, row: str, family: str, stage: str, prompt_path: str,
    writes: Sequence[str] = (), runner: Optional[Callable[..., Any]] = None, session_id: Optional[str] = None,
) -> dict:
    """Run the vendor CLI for one family stage; return the family result dict.

    `family_authored` is true only when the CLI exited 0 and, for a `write` stage, at least one declared
    path's post fingerprint differs from its pre (both taken here, never reported by the runner). The diff is
    attributed by time window, not by writer: a concurrent edit to a declared path while the CLI runs
    (timeout up to 3600s) is credited as CLI-authored. `runner`
    replaces the CLI spawn: called as `runner(prompt, backend=, repo_root=, artifact_dir=, parent_session_id=)`
    (`resolve_session_id`: `session_id`, else env, else the run's recorded launcher), it returns None
    or a dict with `argv`, `exit_code` and `error`. The invocation record is written even when the spawn
    raises; a spawn failure is a FAILED result, never an exception.
    """
    root = Path(root)
    paths = [p for p in writes if p]
    rpath = rec_mod.record_path(root, run_id, row, stage)
    out_path, err_path = rec_mod.raw_stdout_path(root, run_id, row, stage), rec_mod.raw_stderr_path(root, run_id, row, stage)
    artifact_dir = rpath.parent / f"{stage}.artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for name in _ARTIFACT_FILES:
        (artifact_dir / name).unlink(missing_ok=True)
    started = _now()
    pre = {p: rec_mod.fingerprint(root, p) for p in paths}
    argv: list = []
    exit_code = 1
    error: Optional[str] = None
    model: Optional[str] = None
    record: dict = {}
    raised = False
    try:
        if family not in _BACKENDS:
            raise ValueError(f"family {family!r} has no vendor CLI; expected one of {'|'.join(_BACKENDS)}")
        if family == "codex" and stage == "write" and sys.platform == "win32":
            raise RuntimeError(_WIN32_CODEX_WRITE)
        prompt = (root / prompt_path).read_text(encoding="utf-8")
        ran = (runner or _default_runner)(
            prompt, backend=family, repo_root=root, artifact_dir=artifact_dir,
            parent_session_id=resolve_session_id(root, run_id, session_id),
        )
        info = ran if isinstance(ran, dict) else {}
        argv = [str(a) for a in info.get("argv") or []]
        error = info.get("error") or None
        exit_code = int(info.get("exit_code", 0))
        if error and exit_code == 0:
            exit_code = 1
    except Exception as exc:  # noqa: BLE001 -- every failure becomes a recorded FAILED result
        error = f"{type(exc).__name__}: {exc}"[:600]
        exit_code = 1
        raised = True
    finally:
        post = {p: rec_mod.fingerprint(root, p) for p in paths}
        _capture(artifact_dir / "stdout.jsonl", out_path)
        _capture(artifact_dir / "stderr.txt", err_path)
        model = _vendor_model(artifact_dir, family)
        record = {
            "family": family, "stage": stage, "argv": argv, "exit_code": exit_code, "started_at": started,
            "ended_at": _now(), stage_family.VENDOR_MODEL_KEY: model, "pre": pre, "post": post, "error": error,
        }
        replace_text(rpath, json.dumps(record, indent=1))
    changed = rec_mod.authored_paths(record)
    reason = error
    if exit_code == 0 and stage == "write" and not changed:
        reason = _UNCHANGED
    authored = exit_code == 0 and (stage != "write" or bool(changed))
    result: dict = {
        "family": family, "stage": stage, "exit_code": exit_code, "output_path": out_path.relative_to(root).as_posix(),
        "verdict": "OK" if authored else "FAILED" if exit_code else "BLOCKED",
        stage_family.VENDOR_MODEL_KEY: model, stage_family.FAMILY_AUTHORED_KEY: authored,
    }
    if exit_code != 0:
        result["cli_failure"], reason = _classify_failure(artifact_dir, error or f"{family} exited {exit_code}", raised)
    if reason and not authored:
        result["blocked_reason"] = reason
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry: print the family result JSON; exit 0 only when `family_authored`, else 1; 2 on a usage error."""
    ap = argparse.ArgumentParser(prog="family_invoke")
    ap.add_argument("--root", default=".")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--row", required=True)
    ap.add_argument("--family", required=True)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument(stage_family.SESSION_ID_FLAG, dest="session_id", default=None)
    ap.add_argument("--writes", nargs="*", default=[])
    ns = ap.parse_args(argv)
    result = invoke(Path(ns.root), ns.run_id, ns.row, ns.family, ns.stage, ns.prompt, ns.writes, session_id=ns.session_id)
    print(json.dumps(result))
    return 0 if result[stage_family.FAMILY_AUTHORED_KEY] else 1


if __name__ == "__main__":
    raise SystemExit(main())
