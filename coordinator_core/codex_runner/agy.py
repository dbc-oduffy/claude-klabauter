"""codex_runner.agy -- read-only Antigravity (agy) review over one runner-owned stream-json process.

`AgyConversation` owns one `agy --input-format stream-json --output-format stream-json -p=
--mode plan --sandbox` child per conversation and never attaches to a human's session. Each
`send` writes one NDJSON user line and reads stdout until the `{"event":"result"}` line.
Result `usage` is cumulative per conversation: the last figure is stored, per-turn cost is the
difference of successive figures, and nothing is ever summed. The artifact text is inlined in
the turn because headless agy auto-denies tool reads. Default auth is the PM's agy sign-in and
GEMINI_API_KEY is scrubbed; only under the allow_api_key flag is the key read (per-OS resolver)
and set on the child env with `--settings {"modelProvider":"gemini"}`, never to a file, log or argv.
Google calls wrapping agy on consumer plans unsupported (DoE W5.8); the PM accepted it as a trial.
"""
from __future__ import annotations

from coordinator_core.codex_runner.identity_env import provider_identity_env

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import jsonschema

from coordinator_core.codex_runner.contract import (
    FALLBACK_REVIEWER,
    STATUS_FAILED,
    STATUS_LIMIT_EXHAUSTED,
    STATUS_OK,
    OnSpawn,
    RunRequest,
    RunResult,
    findings_schema_path,
)
from coordinator_core.codex_runner.resolve import CodexUnavailable, allow_api_key, resolve_native_cli
from coordinator_core.codex_runner.review import _ENV_ALLOW_WINDOWS as _REVIEW_ENV_ALLOW_WINDOWS

AGY_TIMEOUT_S = 900
CLOSE_GRACE_S = 10
MAX_ARTIFACT_CHARS = 200_000
EVENTS_FILE = "agy-events.jsonl"
STDERR_FILE = "agy-stderr.txt"

CONFINEMENT_BASIS = "artifact inlined in the turn; agy --mode plan --sandbox; no tool reads to audit"

_OK_STATUSES = frozenset({"success", "ok", "completed", "done"})
_LIMIT_MARKERS = (
    "resource_exhausted",
    "usage limit",
    "rate limit",
    "rate_limit",
    "quota",
    "prepayment",
)

API_KEY_ENV = "GEMINI_API_KEY"
KEYCHAIN_SERVICE = "gemini-use-api-key"
EXHAUSTION_REMEDY = "add prepay credit at ai.studio/projects"

_ENV_ALLOW = ("PATH", "HOME", "USERPROFILE", "LANG")
_ENV_ALLOW_WINDOWS = _REVIEW_ENV_ALLOW_WINDOWS

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


class AgyUnavailable(CodexUnavailable):
    """The `agy` binary is not on PATH."""


class AgyError(RuntimeError):
    """The agy process died, timed out or broke the stream-json protocol mid-conversation."""


def resolve_agy() -> list[str]:
    """Return [agy_path] from PATH, else ~/.agy/bin; raise AgyUnavailable when both miss."""
    found = resolve_native_cli("agy")
    if not found:
        raise AgyUnavailable("agy not found on PATH or in ~/.agy/bin (install Antigravity CLI)")
    return [found]


def _build_env(
    environ: Optional[dict] = None, windows: Optional[bool] = None, api_key: Optional[str] = None
) -> dict[str, str]:
    """Allowlisted child env; GEMINI_API_KEY is present only when `api_key` is passed (flag on)."""
    environ = os.environ if environ is None else environ
    windows = sys.platform.startswith("win") if windows is None else windows
    keys = _ENV_ALLOW + (_ENV_ALLOW_WINDOWS if windows else ())
    env = {k: environ[k] for k in keys if environ.get(k)}
    if api_key:
        env[API_KEY_ENV] = api_key
    return provider_identity_env(env, environ, provider="agy", role="reviewer")


def read_api_key(platform: Optional[str] = None, environ: Optional[dict] = None, run=subprocess.run) -> Optional[str]:
    """Paid-route key: macOS keychain service `gemini-use-api-key`; other platforms the parent env only. None when absent."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        try:
            cp = run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=10, shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        key = (cp.stdout or "").strip()
        return key if cp.returncode == 0 and key else None
    return (environ.get(API_KEY_ENV) or "").strip() or None


def build_argv(prefix: list[str], schema: Optional[Path] = None, api_key_route: bool = False) -> list[str]:
    """The agy spawn argv: stream-json both ways, read-only plan mode in the sandbox."""
    argv = [
        *prefix,
        "--input-format", "stream-json", "--output-format", "stream-json", "-p=",
        "--mode", "plan", "--sandbox",
    ]
    if api_key_route:
        argv += ["--settings", json.dumps({"modelProvider": "gemini"})]
    if schema is not None:
        argv += ["--json-schema", str(schema)]
    return argv


def usage_delta(prev: Optional[dict], cur: Optional[dict]) -> Optional[dict]:
    """Per-turn usage: numeric fields of `cur` minus `prev` (cumulative envelopes); None without `cur`."""
    if not isinstance(cur, dict):
        return None
    prev = prev if isinstance(prev, dict) else {}
    out: dict[str, Any] = {}
    for k, v in cur.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        p = prev.get(k)
        out[k] = v - p if isinstance(p, (int, float)) and not isinstance(p, bool) else v
    return out


def _tail(path: Path, limit: int = 600) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
    except OSError:
        return ""


class AgyConversation:
    """One runner-owned agy stream-json process; `send` is one turn, turns share the process."""

    def __init__(
        self,
        run_dir: Path,
        argv_prefix: Optional[list[str]] = None,
        schema: Optional[Path] = None,
        timeout_s: float = AGY_TIMEOUT_S,
        on_spawn: Optional[OnSpawn] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._api_key = api_key
        self.run_dir = Path(run_dir)
        self._prefix = argv_prefix
        self._schema = schema
        self._timeout_s = timeout_s
        self._on_spawn = on_spawn
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.usage: Optional[dict[str, Any]] = None
        self.turn_usage: list[Optional[dict[str, Any]]] = []
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._events_fh = None
        self._stderr_fh = None

    @property
    def stderr_path(self) -> Path:
        return self.run_dir / STDERR_FILE

    def open(self) -> "AgyConversation":
        """Spawn the process (raises AgyUnavailable / AgyError) and call on_spawn(pid) once."""
        prefix = self._prefix if self._prefix is not None else resolve_agy()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events_fh = open(self.run_dir / EVENTS_FILE, "wb")
        self._stderr_fh = open(self.stderr_path, "wb")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        try:
            self.proc = subprocess.Popen(
                build_argv(prefix, self._schema, bool(self._api_key)), cwd=str(self.run_dir),
                env=_build_env(api_key=self._api_key), shell=False,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_fh, creationflags=flags,
            )
        except OSError as exc:
            self._close_files()
            raise AgyError(f"agy spawn failed: {exc}") from exc
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        if self._on_spawn is not None:
            try:
                self._on_spawn(self.pid)
            except BaseException:
                self.kill()
                self.close()
                raise
        return self

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            if self._events_fh is not None:
                self._events_fh.write(raw)
                self._events_fh.flush()
            self._lines.put(raw.decode("utf-8", errors="replace"))
        self._lines.put(None)

    def send(self, content: str) -> dict[str, Any]:
        """Write one user turn; return the `result` object of its `{"event":"result"}` line."""
        if self.proc is None or self.proc.stdin is None:
            raise AgyError("conversation is not open")
        line = json.dumps({"event": "user", "message": {"content": content}}) + "\n"
        try:
            self.proc.stdin.write(line.encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AgyError(f"agy stdin closed: {exc}") from exc
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                raw = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self.kill()
                raise AgyError(f"agy timed out after {self._timeout_s:g}s") from None
            if raw is None:
                rc = self.proc.wait()
                raise AgyError(f"agy exited {rc} before a result event: {_tail(self.stderr_path)}")
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("event") == "result":
                result = ev.get("result")
                result = result if isinstance(result, dict) else {}
                cur = result.get("usage")
                self.turn_usage.append(usage_delta(self.usage, cur))
                if isinstance(cur, dict):
                    self.usage = cur
                return result

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    def close(self) -> Optional[int]:
        """Close stdin, wait briefly, kill if still alive; return the exit code."""
        rc = None
        if self.proc is not None:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            except OSError:
                pass
            try:
                rc = self.proc.wait(timeout=CLOSE_GRACE_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                rc = self.proc.wait()
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        if self._reader is not None:
            self._reader.join(timeout=CLOSE_GRACE_S)
        self._close_files()
        return rc

    def _close_files(self) -> None:
        for fh in (self._events_fh, self._stderr_fh):
            if fh is not None:
                fh.close()
        self._events_fh = self._stderr_fh = None

    def __enter__(self) -> "AgyConversation":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()


def _artifact_text(artifact: str) -> tuple[str, str]:
    """(name, inlined text) of a file, or of every text file under a directory with path headers."""
    src = Path(artifact)
    if not src.exists():
        raise FileNotFoundError(f"artifact not found: {artifact}")
    if src.is_dir():
        parts = []
        for p in sorted(x for x in src.rglob("*") if x.is_file()):
            try:
                body = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            parts.append(f"--- {p.relative_to(src).as_posix()} ---\n{body}")
        text = "\n".join(parts)
    else:
        text = src.read_text(encoding="utf-8")
    if len(text) > MAX_ARTIFACT_CHARS:
        raise ValueError(f"artifact exceeds {MAX_ARTIFACT_CHARS} chars; too large to inline")
    return src.name, text


def review_prompt(name: str, text: str) -> str:
    """The single review turn: instruction plus the artifact text inline."""
    return (
        f"Review the artifact `{name}` inlined below. Do not read or run anything else. "
        "Report concrete defects as findings and give an overall verdict (OK, WARN or FAIL), "
        "as JSON matching the supplied output schema.\n\n"
        f"=== BEGIN {name} ===\n{text}\n=== END {name} ==="
    )


def parse_findings(response: Any) -> dict[str, Any]:
    """Schema-valid findings payload from a result `response` (JSON text, fenced or not, or a dict)."""
    payload = response
    if isinstance(response, str):
        text = response.strip()
        fence = _FENCE_RE.match(text)
        payload = json.loads(fence.group(1) if fence else text)
    schema = json.loads(findings_schema_path().read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)
    return payload


def _limit_text(*texts: Any) -> Optional[str]:
    for t in texts:
        s = t if isinstance(t, str) else json.dumps(t) if t else ""
        if any(m in s.lower() for m in _LIMIT_MARKERS):
            return s.strip()
    return None


def _failed(run_id: str, error: str, **kw: Any) -> RunResult:
    return RunResult(run_id=run_id, status=STATUS_FAILED, error=error, **kw)


def _limited(run_id: str, text: str, **kw: Any) -> RunResult:
    return RunResult(run_id=run_id, status=STATUS_LIMIT_EXHAUSTED, fallback=FALLBACK_REVIEWER, error=text, **kw)


def review_run(
    req: RunRequest,
    run_dir: Path,
    on_spawn: OnSpawn,
    argv_prefix: Optional[list[str]] = None,
) -> RunResult:
    """One read-only agy review turn over a dedicated conversation; on_spawn(agy_pid) fires once after Popen."""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    try:
        name, text = _artifact_text(req.artifact or "")
        prefix = argv_prefix if argv_prefix is not None else resolve_agy()
    except (OSError, ValueError, CodexUnavailable) as exc:
        return _failed(run_id, f"{type(exc).__name__}: {exc}")

    api_key = None
    paid = allow_api_key(requested=req.allow_api_key)
    remedy = f" -- remedy: {EXHAUSTION_REMEDY}" if paid else ""
    if paid:
        api_key = read_api_key()
        if api_key is None:
            return _failed(
                run_id,
                f"allow_api_key is on but no {API_KEY_ENV} source is available "
                f"(macOS keychain service {KEYCHAIN_SERVICE}; other platforms env {API_KEY_ENV})",
            )

    conv = AgyConversation(run_dir, prefix, findings_schema_path(), on_spawn=on_spawn, api_key=api_key)
    started = time.perf_counter()
    try:
        conv.open()
        result = conv.send(review_prompt(name, text))
    except AgyError as exc:
        conv.close()
        pid = conv.pid
        limit = _limit_text(str(exc))
        if limit is not None:
            return _limited(run_id, limit + remedy, codex_pid=pid, usage=conv.usage)
        return _failed(run_id, str(exc), codex_pid=pid, usage=conv.usage)
    elapsed = round(time.perf_counter() - started, 3)
    conv.close()
    pid = conv.pid
    usage = dict(conv.usage) if conv.usage else None
    if usage is not None:
        usage["per_turn"] = conv.turn_usage[-1]
        usage["turn_seconds"] = elapsed

    status = str(result.get("status", "")).lower()
    if status not in _OK_STATUSES:
        err = result.get("error") or result.get("response") or result.get("finished_reason") or status
        stderr = _tail(conv.stderr_path, 2000)
        limit = _limit_text(err, stderr)
        if limit is not None:
            return _limited(run_id, limit + remedy, codex_pid=pid, usage=usage)
        return _failed(run_id, f"agy result status {status or 'missing'}: {err}", codex_pid=pid, usage=usage)
    try:
        payload = parse_findings(result.get("response"))
    except (ValueError, jsonschema.ValidationError) as exc:
        return _failed(
            run_id, f"no schema-valid findings in agy response: {type(exc).__name__}: {exc}",
            codex_pid=pid, usage=usage,
        )
    return RunResult(
        run_id=run_id, status=STATUS_OK, findings=payload["findings"], verdict=payload["verdict"],
        confinement={"breached": False, "paths": [], "basis": CONFINEMENT_BASIS},
        codex_pid=pid, usage=usage,
    )
