"""codex_runner.grok -- read-only Grok Build (`grok -p`) review over one runner-owned streaming-json process.

`review_run` spawns one `grok -p <prompt> --output-format streaming-json --tools Read,Grep`
child per run, never attaches to a human's session and never passes an auto-approve flag. The
artifact text is inlined in the prompt. stdout is read under a monotonic total deadline and a
no-output idle deadline, so silence becomes failed/turn_timeout and never a hang. Each error event
(and the stderr tail of a run that ended without a result) goes through `classify_exit`: a login
cause is STATUS_LOGIN_REQUIRED, a quota cause is STATUS_LIMIT_EXHAUSTED with cause quota_exhausted.
No login probe runs outside this detached worker.
"""
from __future__ import annotations

from coordinator_core.codex_runner.identity_env import provider_identity_env

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import jsonschema

from coordinator_core.agent_pipe.contract import (
    CAUSE_LOGIN_REQUIRED,
    CAUSE_QUOTA,
    CAUSE_TURN_TIMEOUT,
    classify_exit,
    provenance_envelope,
)
from coordinator_core.codex_runner.agy import _artifact_text, _tail, parse_findings, review_prompt
from coordinator_core.codex_runner.contract import (
    FALLBACK_REVIEWER,
    STATUS_FAILED,
    STATUS_LIMIT_EXHAUSTED,
    STATUS_LOGIN_REQUIRED,
    STATUS_OK,
    OnSpawn,
    RunRequest,
    RunResult,
)
from coordinator_core.codex_runner.resolve import CodexUnavailable

GROK_TIMEOUT_S = 900
GROK_IDLE_S = 180
GROK_MAX_TURNS = 8
CLOSE_GRACE_S = 10
EVENTS_FILE = "grok-events.jsonl"
STDERR_FILE = "grok-stderr.txt"

LOGIN_REMEDY = "run `grok login`"
QUOTA_REMEDY = "wait for the rolling free-usage window or add credit"
CONFINEMENT_BASIS = "artifact inlined in the prompt; grok --tools Read,Grep with write/exec/MCP tools denied; no tool reads to audit"
PROVENANCE_SOURCE = "grok -p"

# Write and exec tool rules passed as `--deny`; the explicit tool allowlist is the read-only boundary.
DENY_RULES = ("Edit", "Write", "Bash", "MCPTool")
FORBIDDEN_FLAGS = ("--always-approve", "--dangerously-skip-permissions", "--yolo")

_ENV_ALLOW = ("PATH", "HOME", "USERPROFILE", "LANG", "GROK_HOME")
_ENV_ALLOW_WINDOWS = ("SYSTEMROOT", "APPDATA", "LOCALAPPDATA")
_TEXT_EVENTS = frozenset({"assistant", "message", "text", "agent_message", "agent_message_chunk", "content"})


class GrokUnavailable(CodexUnavailable):
    """The `grok` binary is not on PATH."""


def resolve_grok() -> list[str]:
    """Return [grok_path] from PATH; raise GrokUnavailable when absent."""
    found = shutil.which("grok")
    if not found:
        raise GrokUnavailable("grok not found on PATH (install Grok Build)")
    return [found]


def build_argv(
    prefix: list[str], prompt: str, session_id: Optional[str] = None, max_turns: int = GROK_MAX_TURNS
) -> list[str]:
    """The grok spawn argv: one-shot streaming-json in read-only tool allowlist; auto-approve flags are refused."""
    argv = [
        *prefix, "-p", prompt,
        "--output-format", "streaming-json",
        "--tools", "Read,Grep",
        "--no-subagents", "--no-plan", "--no-auto-update",
        "--max-turns", str(max_turns),
        "-s", session_id or str(uuid.uuid4()),
    ]
    for rule in DENY_RULES:
        argv += ["--deny", rule]
    bad = [f for f in FORBIDDEN_FLAGS if f in argv]
    if bad:
        raise ValueError(f"grok argv must never carry {bad}")
    return argv


def _build_env(environ: Optional[dict] = None, windows: Optional[bool] = None) -> dict[str, str]:
    """Allowlisted child env; no API key is forwarded, so an unauthenticated grok reports login_required."""
    environ = os.environ if environ is None else environ
    windows = sys.platform.startswith("win") if windows is None else windows
    keys = _ENV_ALLOW + (_ENV_ALLOW_WINDOWS if windows else ())
    return provider_identity_env({k: environ[k] for k in keys if environ.get(k)}, environ, provider="grok", role="reviewer")


def _event_text(ev: dict) -> str:
    for key in ("text", "content", "result", "message"):
        v = ev.get(key)
        if isinstance(v, str):
            return v
    return ""


def _failed(run_id: str, error: str, **kw: Any) -> RunResult:
    return RunResult(run_id=run_id, status=STATUS_FAILED, error=error, **kw)


def _limited(run_id: str, text: str, **kw: Any) -> RunResult:
    return RunResult(
        run_id=run_id, status=STATUS_LIMIT_EXHAUSTED, fallback=FALLBACK_REVIEWER,
        error=f"{CAUSE_QUOTA}: {text} -- remedy: {QUOTA_REMEDY}", **kw,
    )


def _login(run_id: str, text: str, **kw: Any) -> RunResult:
    return RunResult(
        run_id=run_id, status=STATUS_LOGIN_REQUIRED, fallback=FALLBACK_REVIEWER,
        error=f"{CAUSE_LOGIN_REQUIRED}: {text} -- remedy: {LOGIN_REMEDY}", **kw,
    )


def _stamped(usage: Optional[dict]) -> dict[str, Any]:
    out = dict(usage) if usage else {}
    out["provenance"] = provenance_envelope("grok", PROVENANCE_SOURCE)
    return out


class _Stream:
    """Outcome of reading one grok process: events seen, final text, first classified error, or a deadline cause."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.result_text: Optional[str] = None
        self.error_cause: Optional[str] = None
        self.error_text: Optional[str] = None
        self.usage: Optional[dict] = None
        self.timeout: Optional[str] = None
        self.rc: Optional[int] = None

    @property
    def response(self) -> str:
        return self.result_text if self.result_text is not None else "".join(self.text_parts)

    def feed(self, raw: str) -> None:
        line = raw.strip()
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        cause = classify_exit(line)
        if cause is not None and self.error_cause is None:
            self.error_cause, self.error_text = cause, _text_of_error(ev, line)
            return
        kind = ev.get("type") or ev.get("event")
        if isinstance(ev.get("usage"), dict):
            self.usage = ev["usage"]
        if kind == "result":
            self.result_text = _event_text(ev)
        elif kind in _TEXT_EVENTS:
            self.text_parts.append(_event_text(ev))


def _text_of_error(ev: dict, line: str) -> str:
    err = ev.get("error")
    for v in (err, ev.get("message")):
        if isinstance(v, dict):
            v = v.get("message") or json.dumps(v)
        if isinstance(v, str) and v:
            return v
    return line[:600]


def _read_stream(proc: subprocess.Popen, events_path: Path, timeout_s: float, idle_s: float) -> _Stream:
    """Drain proc.stdout under total and idle deadlines; kill on either. Never blocks past the deadline."""
    lines: "queue.Queue[Optional[bytes]]" = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    st = _Stream()
    start = last = time.monotonic()
    with open(events_path, "wb") as events:
        while True:
            now = time.monotonic()
            wait = min(start + timeout_s - now, last + idle_s - now)
            if wait <= 0:
                st.timeout = f"{CAUSE_TURN_TIMEOUT}: grok produced no result within "
                st.timeout += f"{timeout_s:g}s" if now - start >= timeout_s else f"{idle_s:g}s of silence"
                proc.kill()
                break
            try:
                raw = lines.get(timeout=wait)
            except queue.Empty:
                continue
            if raw is None:
                break
            last = time.monotonic()
            events.write(raw)
            events.flush()
            st.feed(raw.decode("utf-8", errors="replace"))
    try:
        st.rc = proc.wait(timeout=CLOSE_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        st.rc = proc.wait()
    return st


def review_run(
    req: RunRequest,
    run_dir: Path,
    on_spawn: OnSpawn,
    argv_prefix: Optional[list[str]] = None,
    timeout_s: Optional[float] = None,
    idle_s: Optional[float] = None,
) -> RunResult:
    """One read-only grok review turn; on_spawn(grok_pid) fires once after Popen. Login, quota and silence are named outcomes."""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    try:
        name, text = _artifact_text(req.artifact or "")
        prefix = argv_prefix if argv_prefix is not None else resolve_grok()
    except (OSError, ValueError, CodexUnavailable) as exc:
        return _failed(run_id, f"{type(exc).__name__}: {exc}")

    prompt = review_prompt(name, text) + (
        "\n\nReply with only a JSON object {\"verdict\": \"OK|WARN|FAIL\", \"findings\": "
        "[{\"severity\", \"title\", \"location\", \"detail\"}]}."
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
    stderr_path = run_dir / STDERR_FILE
    try:
        with open(stderr_path, "wb") as err_fh:
            proc = subprocess.Popen(
                build_argv(prefix, prompt), cwd=str(run_dir), env=_build_env(), shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err_fh, creationflags=flags,
            )
    except OSError as exc:
        return _failed(run_id, f"grok spawn failed: {exc}")
    pid = proc.pid
    try:
        on_spawn(pid)
    except BaseException:
        proc.kill()
        proc.wait()
        raise

    started = time.perf_counter()
    st = _read_stream(
        proc, run_dir / EVENTS_FILE,
        GROK_TIMEOUT_S if timeout_s is None else timeout_s, GROK_IDLE_S if idle_s is None else idle_s,
    )
    if proc.stdout is not None:
        proc.stdout.close()
    usage = _stamped(st.usage)
    usage["turn_seconds"] = round(time.perf_counter() - started, 3)
    kw = {"codex_pid": pid, "usage": usage}

    stderr = _tail(stderr_path, 2000)
    cause, detail = st.error_cause, st.error_text
    if cause is None and not st.response.strip():
        cause = classify_exit(stderr)
        detail = stderr
    if cause == CAUSE_LOGIN_REQUIRED:
        return _login(run_id, detail or "not logged in", **kw)
    if cause == CAUSE_QUOTA:
        return _limited(run_id, detail or "quota exhausted", **kw)
    if st.timeout:
        return _failed(run_id, st.timeout, **kw)
    if cause is not None:
        return _failed(run_id, f"{cause}: {detail}", **kw)
    if not st.response.strip():
        return _failed(run_id, f"grok exited {st.rc} without a result: {stderr}", **kw)
    try:
        payload = parse_findings(st.response)
    except (ValueError, jsonschema.ValidationError) as exc:
        return _failed(run_id, f"no schema-valid findings in grok response: {type(exc).__name__}: {exc}", **kw)
    return RunResult(
        run_id=run_id, status=STATUS_OK, findings=payload["findings"], verdict=payload["verdict"],
        confinement={"breached": False, "paths": [], "basis": CONFINEMENT_BASIS}, **kw,
    )
