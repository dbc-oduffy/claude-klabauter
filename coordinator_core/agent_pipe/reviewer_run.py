"""agent_pipe.reviewer_run -- run a named reviewer out of process on a non-Anthropic provider's CLI.

The prompt is the reviewer's own agent-definition body plus the brief plus a return-shape instruction.
The provider CLI is spawned from an argv list (no shell) with its model flag; its final message must
carry a JSON object `{"verdict": ..., "findings": [...]}`. Any provider failure -- missing CLI,
non-zero exit, timeout, no reply, unparseable reply -- returns verdict FAILED with the stderr tail,
never a silent pass. An externally run reviewer returns findings only; it cannot apply them in place.

CLI: `python -m coordinator_core.agent_pipe.reviewer_run <agent_type> <provider> <model> [<verdicts>]`
reads the brief on stdin and prints the result dict as one JSON line. `<verdicts>` is a comma list of
the verdicts the reviewer's schema allows; a reply outside it is FAILED.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from coordinator_core.agent_pipe.agy import AgyAdapter
from coordinator_core.agent_pipe.codex import CodexAdapter
from coordinator_core.agent_pipe.reviewer_provider import ProviderChoice
from coordinator_core.hooks.block_unenumerated_agent_type import plugin_agent_model

FAILED = "FAILED"
DEFAULT_TIMEOUT_S = 1800.0
_STDERR_TAIL_CHARS = 2000

RETURN_INSTRUCTION = (
    "\n\n---\nReturn protocol: you run read-only and cannot apply fixes or write files. End your final "
    'message with one JSON object, in a ```json fence: {"verdict": "<your verdict>", "findings": '
    '[{"severity": "...", "location": "...", "finding": "..."}]}.'
)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)


def _codex_argv(prompt: str, model: str) -> list[str]:
    argv = CodexAdapter().spawn_argv(prompt)
    return [*argv[:-1], "--sandbox", "read-only", "--ephemeral", "-m", model, argv[-1]]


def _agy_argv(prompt: str, model: str) -> list[str]:
    return [*AgyAdapter().spawn_argv(prompt), "--mode", "plan", "--sandbox", "--model", model]


def _grok_argv(prompt: str, model: str) -> list[str]:
    from coordinator_core.codex_runner.grok import build_argv, resolve_grok

    return [*build_argv(resolve_grok(), prompt), "--model", model]


class _AgyMessages:
    """Preserve streaming delta whitespace, including spaces inside JSON string values."""
    def parse_line(self, line: str):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        step = event.get("step_update") if isinstance(event, dict) else None
        if isinstance(step, dict) and step.get("step_type") == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str):
                return "", delta
        return None


class _GrokMessages:
    def parse_line(self, line: str):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict) or (event.get("type") or event.get("event")) not in ("result", "assistant", "message", "text", "agent_message", "agent_message_chunk", "content"):
            return None
        for key in ("result", "text", "content", "message"):
            text = event.get(key)
            if isinstance(text, str) and text:
                return "", text
        return None


# provider -> (argv builder, adapter factory whose parse_line yields (session, text) messages).
_PROVIDERS: dict[str, tuple[Callable[[str, str], list[str]], Callable[[], Any]]] = {
    "codex": (_codex_argv, CodexAdapter),
    "agy": (_agy_argv, _AgyMessages),
    "grok": (_grok_argv, _GrokMessages),
}


def _failed(choice: ProviderChoice, error: str) -> dict:
    return {
        "verdict": FAILED,
        "findings": [],
        "provider": choice.provider,
        "model": choice.model,
        "error": error,
    }


def _definition_body(agent_type: str, home: Any) -> Optional[str]:
    kwargs = {} if home is None else {"home": home}
    _, path = plugin_agent_model(agent_type, **kwargs)
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def _verdict_from(text: str) -> Optional[dict]:
    """The last JSON object in `text` that carries a string `verdict`, else None."""
    decoder = json.JSONDecoder()
    candidates = []
    offset = 0
    while offset < len(text):
        starts = [pos for pos in (text.find("{", offset), text.find("[", offset)) if pos >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            obj, consumed = decoder.raw_decode(text[start:])
        except ValueError:
            offset = start + 1
            continue
        # A decoded object is one result candidate. Its nested objects belong to that
        # result, and must never be reinterpreted as independent reviewer replies.
        offset = start + consumed
        if isinstance(obj, dict) and isinstance(obj.get("verdict"), str) and obj["verdict"].strip():
            candidates.append(obj)
    return candidates[-1] if candidates else None


def _last_message(stdout: str, adapter: Any) -> Optional[str]:
    messages = []
    for line in stdout.splitlines():
        parsed = adapter.parse_line(line)
        if parsed:
            messages.append(parsed[1])
    return "".join(messages) or None


def _provider_env(provider: str) -> Optional[dict[str, str]]:
    """Use the runner's authenticated-local-account policy, not ambient paid API keys."""
    if provider == "codex":
        from coordinator_core.codex_runner.review import _unattended_env
        return _unattended_env()
    if provider == "agy":
        from coordinator_core.codex_runner.agy import _build_env
        return _build_env()
    if provider == "grok":
        from coordinator_core.codex_runner.grok import _build_env
        return _build_env()
    return None


def run_reviewer(
    agent_type: str,
    brief: str,
    choice: ProviderChoice,
    *,
    home: Any = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    verdicts: Optional[Sequence[str]] = None,
    on_spawn: Optional[Callable[[int], None]] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Run `agent_type` on `choice` and return `{verdict, findings, provider, model}`.

    Failure returns `{verdict: "FAILED", findings: [], provider, model, error}`; `error` is the
    provider's stderr tail, or the reason no provider process produced a verdict. When `verdicts` is
    given, a verdict outside it is FAILED.
    """
    spec = _PROVIDERS.get(choice.provider)
    if spec is None:
        return _failed(choice, f"no headless argv adapter for provider {choice.provider!r}")
    body = _definition_body(agent_type, home)
    if not body:
        return _failed(choice, f"agent definition for {agent_type!r} not found")
    build_argv, make_adapter = spec
    try:
        argv = build_argv(f"{body}\n\n---\n{brief}{RETURN_INSTRUCTION}", choice.model)
        child_env = _provider_env(choice.provider)
        if choice.provider == "codex":
            from coordinator_core.codex_runner.codex_spawn_env import shape_codex_child
            from coordinator_core.codex_runner.sandbox_preflight import SandboxSetupRefused
            try:
                child_env = shape_codex_child(child_env, writable_root=None)
            except SandboxSetupRefused as exc:
                return _failed(choice, str(exc))
        proc = subprocess.Popen(
            argv,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=child_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            if on_spawn is not None:
                on_spawn(proc.pid)
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
    except subprocess.TimeoutExpired:
        return _failed(choice, f"provider timed out after {timeout:g}s")
    except Exception as exc:  # CLI missing (CodexUnavailable, FileNotFoundError) or unspawnable
        return _failed(choice, f"provider could not be started: {exc}")
    tail = (stderr or "")[-_STDERR_TAIL_CHARS:]
    if proc.returncode != 0:
        return _failed(choice, tail or f"provider exited {proc.returncode}")
    reply = _last_message(stdout or "", make_adapter())
    parsed = _verdict_from(reply) if reply else None
    if parsed is None:
        return _failed(choice, f"unparseable reviewer reply; stderr: {tail}".rstrip())
    verdict = parsed["verdict"].strip()
    if verdicts and verdict not in verdicts:
        return _failed(choice, f"verdict {verdict!r} not in the schema's {sorted(verdicts)}")
    findings = parsed.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        return _failed(choice, "reviewer reply must contain a findings array of objects")
    return {
        "verdict": verdict,
        "findings": findings,
        "provider": choice.provider,
        "model": choice.model,
    }


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (3, 4):
        print("usage: reviewer_run <agent_type> <provider> <model> [<verdicts>]  (brief on stdin)", file=sys.stderr)
        return 2
    agent_type, provider, model = args[:3]
    verdicts = [v for v in args[3].split(",") if v] if len(args) == 4 else None
    result = run_reviewer(agent_type, sys.stdin.read(), ProviderChoice(provider, model), verdicts=verdicts)
    print(json.dumps(result))
    return 0 if result["verdict"] != FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
