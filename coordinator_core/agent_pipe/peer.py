"""agent_pipe.peer -- peer-to-peer relay between headless agent participants, no Claude hop.

A participant is a named headless CLI session. A message it emits that begins `@<name> ` is
delivered to participant `<name>` through that peer's native primitive: stdin-mode participants
(agy `--input-format stream-json`) get one JSON turn line on stdin; resume-mode participants
(codex) get a fresh `resume_argv` child on their session id. A name in `observers` maps to a
Claude session id and the message lands in that session's mailbox, which `mailbox_line` wakes on.
Every message is also appended to the `log_session` mailbox.

relay() blocks on the calling thread and returns when the conversation is quiet, hits the hop
cap, hits the deadline, or every child has exited; all children are reaped before it returns.
No resident process. Trap: stdin-mode turns have no end marker, so "quiet" is `idle` seconds
without any message while no resume child runs.
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional

from coordinator_core.agent_pipe import mailbox
from coordinator_core.agent_pipe.contract import Adapter, PipeMessage
from coordinator_core.win_portability import no_console_creationflags

STDIN = "stdin"
RESUME = "resume"

_ADDRESS = re.compile(r"^@([A-Za-z0-9_.-]+)[\s:]+(.*)$", re.DOTALL)
_POLL_SECONDS = 0.02
_REAP_SECONDS = 5.0


def json_turn(text: str) -> str:
    """The stdin turn line for agy's stream-json input: one JSON object per turn."""
    return json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False)


@dataclass
class Participant:
    """One named session. `argv` overrides the first spawn (stdin mode needs it: no `-p` prompt)."""

    name: str
    adapter: Adapter
    prompt: str = ""
    mode: str = RESUME
    argv: Optional[List[str]] = None
    conversation: str = ""
    turn_line: Callable[[str], str] = json_turn
    inbox: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue, repr=False)


@dataclass
class RelayResult:
    stopped_by: str
    hops: int
    pids: List[int]
    exit_codes: Dict[str, Optional[int]]
    conversations: Dict[str, str]


def agy_participant(name: str, prompt: str = "", conversation: str = "") -> Participant:
    """agy as a long-lived stdin-mode participant; the first turn is `prompt`."""
    from coordinator_core.agent_pipe.agy import AgyAdapter

    argv = [shutil.which("agy") or "agy"]
    if conversation:
        argv += ["--conversation", conversation]
    argv += ["--input-format", "stream-json", "--output-format", "stream-json"]
    return Participant(name, AgyAdapter(), prompt, STDIN, argv, conversation)


def codex_participant(name: str, prompt: str = "", conversation: str = "") -> Participant:
    """codex as a resume-mode participant: first run is `exec <prompt>`, each delivery `exec resume`."""
    from coordinator_core.agent_pipe.codex import CodexAdapter

    return Participant(name, CodexAdapter(), prompt, RESUME, None, conversation)


_GROK_TEXT_EVENTS = frozenset({"assistant", "message", "text", "agent_message", "agent_message_chunk", "content"})


class GrokAdapter:
    """Headless grok (`grok -p ... --output-format streaming-json`, read-only `--tools` allowlist, write/exec denied) as a resume-mode adapter.

    The session id is ours (`-s`), so every message is attributed to it. Text events are messages;
    a `result` event counts only when no text event came first. Trap: the streaming-json event
    schema is read off codex_runner.grok, not a captured grok run.
    """

    def __init__(self) -> None:
        self.session = ""
        self._texted = False

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> List[str]:
        from coordinator_core.codex_runner.grok import build_argv, resolve_grok

        self.session = conversation or str(uuid.uuid4())
        return build_argv(resolve_grok(), prompt, self.session)

    def resume_argv(self, conversation: str, text: str) -> List[str]:
        return self.spawn_argv(text, conversation)

    def parse_line(self, line: str):
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(ev, dict) or not self.session:
            return None
        kind = ev.get("type") or ev.get("event")
        if kind in _GROK_TEXT_EVENTS:
            self._texted = True
        elif not (kind == "result" and not self._texted):
            return None
        for key in ("text", "content", "result", "message"):
            v = ev.get(key)
            if isinstance(v, str) and v.strip():
                return self.session, v.strip()
        return None


def grok_participant(name: str, prompt: str = "", conversation: str = "") -> Participant:
    """grok as a resume-mode participant: first run is `grok -p <prompt>`, each delivery a fresh run on the same `-s` session."""
    return Participant(name, GrokAdapter(), prompt, RESUME, None, conversation)


class _Relay:
    def __init__(self, participants, observers, log_session, idle, deadline, max_hops):
        names = [p.name for p in participants]
        if len(set(names)) != len(names):
            raise ValueError("duplicate participant name")
        for p in participants:
            if p.mode not in (STDIN, RESUME):
                raise ValueError(f"unknown mode: {p.mode!r}")
        self.by_name = {p.name: p for p in participants}
        self.observers = dict(observers or {})
        clash = set(self.observers) & set(self.by_name)
        if clash:
            raise ValueError(f"observer name collides with participant: {sorted(clash)}")
        self.log_session = log_session
        self.idle = idle
        self.deadline = deadline
        self.max_hops = max_hops
        self.lock = threading.Lock()
        self.hops = 0
        self.pending = 0
        self.last_activity = time.monotonic()
        self.stop_reason: Optional[str] = None
        self.procs: List[subprocess.Popen] = []
        self.pids: List[int] = []
        self.exit_codes: Dict[str, Optional[int]] = {n: None for n in names}
        self.stdin_alive = 0
        self.write_locks = {n: threading.Lock() for n in names}
        self.stdin_procs: Dict[str, subprocess.Popen] = {}

    def _touch(self) -> None:
        self.last_activity = time.monotonic()

    def _spawn(self, argv, stdin) -> subprocess.Popen:
        proc = subprocess.Popen(
            argv,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            **no_console_creationflags(),
        )
        with self.lock:
            self.procs.append(proc)
            self.pids.append(proc.pid)
        return proc

    def _append(self, sender: Participant, to_session: str, content: str) -> None:
        mailbox.append(
            PipeMessage(
                id=uuid.uuid4().hex,
                sender_agent=sender.name,
                sender_conversation=sender.conversation,
                to_session=to_session,
                content=content,
                ts=datetime.now(timezone.utc).isoformat(),
            )
        )

    def _emit(self, sender: Participant, conversation: str, content: str) -> None:
        if conversation:
            sender.conversation = conversation
        self._append(sender, self.log_session, content)
        match = _ADDRESS.match(content)
        if match:
            target, body = match.group(1), match.group(2)
            if target in self.observers:
                if self.observers[target] != self.log_session:
                    self._append(sender, self.observers[target], content)
            elif target in self.by_name and target != sender.name:
                with self.lock:
                    if self.hops >= self.max_hops:
                        self.stop_reason = self.stop_reason or "hop_limit"
                        return
                    self.hops += 1
                    self._touch()
                self._deliver(self.by_name[target], f"[from {sender.name}] {body}")
                return
        with self.lock:
            self._touch()

    def _deliver(self, peer: Participant, text: str) -> None:
        if peer.mode == RESUME:
            with self.lock:
                self.pending += 1
            peer.inbox.put(text)
            return
        proc = self.stdin_procs.get(peer.name)
        if proc is not None:
            self._write_turn(peer, proc, text)

    def _read(self, p: Participant, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            parsed = p.adapter.parse_line(line.rstrip("\r\n"))
            if parsed is not None:
                self._emit(p, *parsed)
        proc.wait()

    def _stdin_main(self, p: Participant, proc: subprocess.Popen) -> None:
        if p.prompt:
            self._write_turn(p, proc, p.prompt)
        try:
            self._read(p, proc)
        finally:
            self.exit_codes[p.name] = proc.returncode
            with self.lock:
                self.stdin_alive -= 1
                self._touch()

    def _write_turn(self, p: Participant, proc: subprocess.Popen, text: str) -> None:
        try:
            with self.write_locks[p.name]:
                proc.stdin.write(p.turn_line(text) + "\n")
                proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            # Trap: a dead stdin peer drops the turn silently; its exit code (set by _stdin_main) is the only record.
            pass

    def _run_once(self, p: Participant, argv: List[str]) -> None:
        try:
            proc = self._spawn(argv, subprocess.DEVNULL)
        except OSError:
            self.exit_codes[p.name] = 127
            return
        self._read(p, proc)
        self.exit_codes[p.name] = proc.returncode

    def _resume_main(self, p: Participant) -> None:
        self._run_once(p, p.argv or p.adapter.spawn_argv(p.prompt, p.conversation or None))
        with self.lock:
            self.pending -= 1
            self._touch()
        while True:
            text = p.inbox.get()
            if text is None:
                return
            if self.stop_reason is None:
                argv = p.adapter.resume_argv(p.conversation, text) if p.conversation else p.adapter.spawn_argv(text)
                self._run_once(p, argv)
            with self.lock:
                self.pending -= 1
                self._touch()

    def _quiet(self) -> bool:
        with self.lock:
            if self.pending > 0:
                return False
            if self.stdin_alive == 0:
                return True
            return time.monotonic() - self.last_activity >= self.idle

    def run(self) -> RelayResult:
        threads: List[threading.Thread] = []
        for p in self.by_name.values():
            if p.mode == STDIN:
                try:
                    proc = self._spawn(p.argv or p.adapter.spawn_argv(p.prompt, p.conversation or None), subprocess.PIPE)
                except OSError:
                    self.exit_codes[p.name] = 127
                    continue
                self.stdin_procs[p.name] = proc
                self.stdin_alive += 1
                t = threading.Thread(target=self._stdin_main, args=(p, proc), daemon=True)
            else:
                self.pending += 1
                t = threading.Thread(target=self._resume_main, args=(p,), daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        start = time.monotonic()
        reason = "quiet"
        try:
            while True:
                time.sleep(_POLL_SECONDS)
                if self.stop_reason:
                    reason = self.stop_reason
                    break
                if time.monotonic() - start >= self.deadline:
                    reason = "deadline"
                    break
                if self._quiet():
                    reason = "exited" if self.stdin_alive == 0 else "quiet"
                    break
        finally:
            self._shutdown(threads, kill=reason == "deadline")
        return RelayResult(
            stopped_by=reason,
            hops=self.hops,
            pids=list(self.pids),
            exit_codes=dict(self.exit_codes),
            conversations={n: p.conversation for n, p in self.by_name.items()},
        )

    def _shutdown(self, threads: List[threading.Thread], kill: bool) -> None:
        with self.lock:
            if self.stop_reason is None:
                self.stop_reason = "shutdown"
        for p in self.by_name.values():
            p.inbox.put(None)
        for proc in list(self.stdin_procs.values()):
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
        if kill:
            self._kill_all()
        end = time.monotonic() + _REAP_SECONDS
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        if any(t.is_alive() for t in threads):
            self._kill_all()
            for t in threads:
                t.join(_REAP_SECONDS)

    def _kill_all(self) -> None:
        with self.lock:
            procs = list(self.procs)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()


def relay(
    participants: List[Participant],
    *,
    observers: Optional[Mapping[str, str]] = None,
    log_session: str = "peer-log",
    idle: float = 1.0,
    deadline: float = 300.0,
    max_hops: int = 16,
) -> RelayResult:
    """Run `participants` until quiet; route `@<name> ` messages between them and to `observers`.

    `observers` maps a roster name to a Claude session id (its mailbox is the wake path).
    `max_hops` bounds peer-to-peer deliveries so two replying agents cannot loop forever.
    """
    return _Relay(participants, observers, log_session, idle, deadline, max_hops).run()


def main(argv: Optional[List[str]] = None) -> int:
    """`python -m coordinator_core.agent_pipe.peer <spec.json>`.

    Spec: {participants: [{name, agent: antigravity|codex, prompt, conversation?}], observers?,
    log_session?, idle?, deadline?, max_hops?}. Prints the RelayResult as JSON.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: python -m coordinator_core.agent_pipe.peer <spec.json>\n")
        return 2
    with open(args[0], encoding="utf-8") as f:
        spec = json.load(f)
    makers = {"antigravity": agy_participant, "codex": codex_participant}
    parts = [
        makers[d["agent"]](d["name"], d.get("prompt", ""), d.get("conversation", ""))
        for d in spec["participants"]
    ]
    opts = {k: spec[k] for k in ("observers", "log_session", "idle", "deadline", "max_hops") if k in spec}
    result = relay(parts, **opts)
    print(json.dumps(result.__dict__))
    return 0


if __name__ == "__main__":
    sys.exit(main())
