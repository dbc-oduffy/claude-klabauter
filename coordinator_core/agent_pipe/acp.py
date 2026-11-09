"""agent_pipe.acp -- generic ACP v1 stdio client implementing session_driver.SessionDriver.

Newline-delimited JSON-RPC 2.0 over a child agent's stdio (`grok agent stdio`, `agy acp`). One reader
thread routes every inbound message: responses to their waiting request, `session/update`
notifications to the active prompt's queue, and agent-to-client requests (`session/request_permission`)
to the approval callback. The default callback is reject_once: nothing is ever allowed by default.
`fs` and `terminal` client capabilities are advertised false and answered method-not-found.
TRAP: a `session/load` replay arrives as updates before the response; with no prompt active they are
dropped. A silent agent is a named `handshake_timeout`, a silent prompt a named `turn_timeout`.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Iterator, Optional

from coordinator_core.agent_pipe.contract import (
    CAUSE_HANDSHAKE_TIMEOUT,
    CAUSE_TURN_TIMEOUT,
    KIND_GROK,
    classify_exit,
)
from coordinator_core.git.agent_provenance import MODEL_ENV, SESSION_ENV, VENDOR_ENV
from coordinator_core.agent_pipe.session_driver import (
    REJECT_ONCE,
    ApprovalCallback,
    Decision,
    PermissionRequest,
    SessionHandle,
    Update,
)

PROTOCOL_VERSION = 1
HANDSHAKE_TIMEOUT_S = 15.0
REQUEST_TIMEOUT_S = 30.0
PROMPT_TIMEOUT_S = 600.0
METHOD_NOT_FOUND = -32601
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CLIENT_INFO = {"name": "coordinator-agent-pipe", "version": "1"}
_CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
_UPDATE_KINDS = {
    "agent_message_chunk": "message",
    "agent_thought_chunk": "thought",
    "tool_call": "tool_call",
    "tool_call_update": "tool_call",
}


class AcpError(Exception):
    """A JSON-RPC error, a transport loss (code -1) or a named deadline; `cause` is a contract cause or None."""

    def __init__(self, code: int, message: str, cause: Optional[str] = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.cause = cause if cause is not None else classify_exit(message)


def reject_once(request: PermissionRequest) -> Decision:
    """Default approval callback: reject every permission request."""
    return Decision(REJECT_ONCE)


def _to_update(params: dict) -> Update:
    body = params.get("update") if isinstance(params.get("update"), dict) else {}
    name = str(body.get("sessionUpdate", ""))
    content = body.get("content")
    text = content.get("text", "") if isinstance(content, dict) and isinstance(content.get("text"), str) else ""
    return Update(kind=_UPDATE_KINDS.get(name, name or "update"), text=text, raw=body)


def _option_id(options: list, decision: str) -> Optional[str]:
    """The agent option a decision selects: by option id, else by option kind; None when neither matches."""
    for o in options:
        if o.get("optionId") == decision:
            return decision
    for o in options:
        if o.get("kind") == decision and o.get("optionId"):
            return str(o["optionId"])
    return None


def stamped_env(kind: str, model: Optional[str] = None, session: Optional[str] = None) -> dict[str, str]:
    """os.environ plus the launcher provenance stamp; an unknown model or session is removed, never inherited."""
    env = dict(os.environ)
    env[VENDOR_ENV] = kind
    for name, value in ((MODEL_ENV, model), (SESSION_ENV, session)):
        if value:
            env[name] = value
        else:
            env.pop(name, None)
    return env


class AcpDriver:
    """SessionDriver over one ACP agent child process, spawned lazily on first use.

    `approve` answers each PermissionRequest; a callback that raises, or names an option the agent did
    not offer, rejects. One prompt per session is in flight at a time. `kind` is the vendor stamped
    into the child's environment; it is never inferred from argv.
    """

    def __init__(
        self,
        argv: list[str],
        approve: Optional[ApprovalCallback] = None,
        handshake_timeout_s: float = HANDSHAKE_TIMEOUT_S,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        prompt_timeout_s: float = PROMPT_TIMEOUT_S,
        kind: str = KIND_GROK,
        model: Optional[str] = None,
        session: Optional[str] = None,
    ) -> None:
        self._kind = kind
        self._model = model
        self._session = session
        self._argv = list(argv)
        self._approve = approve or reject_once
        self._handshake_timeout_s = handshake_timeout_s
        self._request_timeout_s = request_timeout_s
        self._prompt_timeout_s = prompt_timeout_s
        self._proc: Optional[subprocess.Popen] = None
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[Any, "queue.Queue[dict]"] = {}
        self._streams: dict[str, "queue.Queue[tuple[str, Any]]"] = {}
        self._cwds: dict[str, str] = {}
        self._dead: Optional[str] = None
        self.capabilities: dict[str, Any] = {}
        self.start_updates: list[Update] = []

    def _connect(self) -> None:
        if self._proc is not None and self._dead is None:
            return
        self._dead = None
        try:
            self._proc = subprocess.Popen(
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW, shell=False,
                env=stamped_env(self._kind, self._model, self._session),
            )
        except OSError as exc:
            raise AcpError(-1, f"cannot spawn {self._argv[0]}: {exc}") from exc
        threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
        try:
            result = self._request(
                "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": _CLIENT_CAPABILITIES, "clientInfo": _CLIENT_INFO},
                self._handshake_timeout_s, timeout_cause=CAUSE_HANDSHAKE_TIMEOUT,
            )
        except AcpError:
            self.shutdown()
            raise
        self.capabilities = (result or {}).get("agentCapabilities") or {}

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._route(msg)
        except (OSError, ValueError):
            pass
        if proc is self._proc:
            self._dead = "agent process closed its stdout"
            for q in list(self._pending.values()):
                q.put({"error": {"code": -1, "message": self._dead}})
            for s in list(self._streams.values()):
                s.put(("error", AcpError(-1, self._dead)))

    def _route(self, msg: dict) -> None:
        method = msg.get("method")
        if method is None:
            q = self._pending.get(msg.get("id"))
            if q is not None:
                q.put(msg)
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if "id" not in msg:
            if method == "session/update":
                s = self._streams.get(str(params.get("sessionId", "")))
                if s is not None:
                    s.put(("update", _to_update(params)))
            return
        if method == "session/request_permission":
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": self._decide(params)}})
        else:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": METHOD_NOT_FOUND, "message": f"{method} not supported"}})

    def _decide(self, params: dict) -> dict:
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]
        tool = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        request = PermissionRequest(
            tool=str(tool.get("title") or tool.get("kind") or ""),
            options=tuple(str(o["optionId"]) for o in options if o.get("optionId")),
        )
        try:
            wanted = self._approve(request).option_id
        except Exception:  # noqa: BLE001 -- a broken callback must reject, never allow
            wanted = REJECT_ONCE
        chosen = _option_id(options, wanted) or _option_id(options, REJECT_ONCE)
        if chosen is None:
            return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": chosen}

    def _send(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpError(-1, "agent is not running")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AcpError(-1, f"agent stdin closed: {exc}") from exc

    def _begin(self, method: str, params: dict) -> tuple[Any, "queue.Queue[dict]"]:
        self._next_id += 1
        rid = self._next_id
        q: "queue.Queue[dict]" = queue.Queue()
        self._pending[rid] = q
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except AcpError:
            self._pending.pop(rid, None)
            raise
        return rid, q

    def _finish(self, rid: Any, msg: dict) -> Any:
        self._pending.pop(rid, None)
        err = msg.get("error")
        if isinstance(err, dict):
            raise AcpError(int(err.get("code", -1)), str(err.get("message", "")))
        return msg.get("result")

    def _request(self, method: str, params: dict, timeout_s: float, timeout_cause: Optional[str] = None) -> Any:
        rid, q = self._begin(method, params)
        try:
            msg = q.get(timeout=timeout_s)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise AcpError(-1, f"{method} timed out after {timeout_s:g}s", timeout_cause or CAUSE_TURN_TIMEOUT) from None
        return self._finish(rid, msg)

    def start(self, cwd: str, prompt: Optional[str] = None) -> SessionHandle:
        """`session/new` in `cwd`; an initial `prompt` is run to its end and its updates kept in `start_updates`."""
        self._connect()
        result = self._request("session/new", {"cwd": cwd, "mcpServers": []}, self._request_timeout_s) or {}
        sid = str(result.get("sessionId") or "")
        if not sid:
            raise AcpError(-1, "session/new returned no sessionId")
        handle = SessionHandle(session_id=sid, cwd=cwd)
        self._cwds[sid] = cwd
        self.start_updates = list(self.prompt(handle, prompt)) if prompt else []
        return handle

    def resume(self, session_id: str) -> SessionHandle:
        """`session/resume`, falling back to `session/load` when the agent lacks it; replayed history is dropped."""
        self._connect()
        cwd = self._cwds.get(session_id) or os.getcwd()
        params = {"sessionId": session_id, "cwd": cwd, "mcpServers": []}
        try:
            self._request("session/resume", params, self._request_timeout_s)
        except AcpError as exc:
            if exc.code != METHOD_NOT_FOUND:
                raise
            self._request("session/load", params, self._request_timeout_s)
        self._cwds[session_id] = cwd
        return SessionHandle(session_id=session_id, cwd=cwd)

    def prompt(self, handle: SessionHandle, text: str) -> Iterator[Update]:
        """Send `session/prompt` and yield its updates; the last is Update('done') carrying the stopReason in `raw`."""
        self._connect()
        stream: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._streams[handle.session_id] = stream
        rid, answer = self._begin(
            "session/prompt", {"sessionId": handle.session_id, "prompt": [{"type": "text", "text": text}]}
        )
        threading.Thread(target=self._relay, args=(rid, answer, stream), daemon=True).start()
        deadline = time.monotonic() + self._prompt_timeout_s
        try:
            while True:
                try:
                    kind, val = stream.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    raise AcpError(-1, f"session/prompt timed out after {self._prompt_timeout_s:g}s", CAUSE_TURN_TIMEOUT) from None
                if kind == "error":
                    raise val
                yield val
                if kind == "done":
                    return
        finally:
            self._streams.pop(handle.session_id, None)
            self._pending.pop(rid, None)

    def _relay(self, rid: Any, answer: "queue.Queue[dict]", stream: "queue.Queue[tuple[str, Any]]") -> None:
        msg = answer.get()
        try:
            result = self._finish(rid, msg) or {}
        except AcpError as exc:
            stream.put(("error", exc))
            return
        stream.put(("done", Update(kind="done", raw=dict(result))))

    def cancel(self, handle: SessionHandle) -> None:
        """`session/cancel` notification; the in-flight prompt ends with stopReason cancelled."""
        self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": handle.session_id}})

    def close(self, handle: SessionHandle) -> None:
        """Close the session (best effort) and shut the agent down once no session remains."""
        self._cwds.pop(handle.session_id, None)
        if self._proc is not None and self._dead is None and (self.capabilities.get("sessionCapabilities") or {}).get("close") is not None:
            try:
                self._request("session/close", {"sessionId": handle.session_id}, 5.0)
            except AcpError:
                pass
        if not self._cwds:
            self.shutdown()

    def shutdown(self) -> None:
        """Kill the agent child and forget it; the next call reconnects."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for pipe in (proc.stdin,):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if proc.stdout is not None:
            proc.stdout.close()

    def __enter__(self) -> "AcpDriver":
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
