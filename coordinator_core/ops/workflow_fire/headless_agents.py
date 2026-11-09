"""Run a Workflow agent through a native CLI, without a Claude Workflow harness.

The Codex launcher and subscription guard are shared with codex_runner. Workflow
model names are vendor hints: Claude tier names never become Codex model IDs;
Codex uses its configured native default unless ``codexModel`` is explicit.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import jsonschema

from coordinator_core.codex_runner.resolve import preflight_auth, resolve_codex
from coordinator_core.codex_runner.review import _unattended_env
from coordinator_core.bash_guards import _heavy_command_class as capacity
from coordinator_core.group_em import admission_post, slot_grant, slot_wait

DEFAULT_AGENT_PEAK_GIB = 1.0


class AgentFailed(RuntimeError):
    """The worker failed, timed out, or returned data outside its schema."""


def _codex_schema(schema: Any) -> Any:
    """Return the unchanged schema only when it already fits native strict output.

    Open maps and optional members are Workflow semantics, not adapter defaults.
    Never close a map or require an optional member just to satisfy the CLI.
    Unsupported schemas use prompt-constrained JSON and original local validation.
    """
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "enum", "const", "description", "title", "anyOf"}

    def supported(node: Any) -> bool:
        if not isinstance(node, dict) or not set(node).issubset(allowed):
            return False
        types = node.get("type")
        types = set(types) if isinstance(types, list) else {types} if isinstance(types, str) else set()
        if "object" in types or "properties" in node:
            properties = node.get("properties")
            if not isinstance(properties, dict) or node.get("additionalProperties") is not False:
                return False
            if set(node.get("required", [])) != set(properties):
                return False
            if not all(supported(value) for value in properties.values()):
                return False
        if "array" in types and ("items" not in node or not supported(node["items"])):
            return False
        if "anyOf" in node:
            alternatives = node["anyOf"]
            if not isinstance(alternatives, list) or not alternatives or not all(map(supported, alternatives)):
                return False
        elif not types:
            return False
        return True

    return schema if isinstance(schema, dict) and schema.get("type") == "object" and supported(schema) else None


def _environment() -> dict[str, str]:
    # Do not inherit API-key opt-ins, parent session identity or Claude nesting.
    source = dict(os.environ)
    source.pop("CODEX_RUNNER_ALLOW_API_KEY", None)
    source.pop("OPENAI_API_KEY", None)
    source.pop("CODEX_API_KEY", None)
    env = _unattended_env(source, requested=False)
    # The executor does not own shared daemon lifecycle. CLI calls run cold and
    # must never start/respawn a warm service as an implicit side effect.
    env['COORDINATOR_WARM'] = '0'
    # Native operation doors validate this stamped build using their normal
    # bootstrap ladder; never silently dispatch back into another live clone.
    env['COORDINATOR_ENGINE_ROOT'] = str(Path(__file__).resolve().parents[3])
    # Native tools/hooks need these Windows and coordinator runtime locations.
    for key in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC",
                "COORDINATOR_SETTINGS_HOME", "MACHINE_LOCAL_REGISTRY_DIR"):
        if source.get(key):
            env[key] = source[key]
    return env


def _cleanup(function, *args, **kwargs) -> None:
    try:
        function(*args, **kwargs)
    except Exception:
        # Run every cleanup leg, preserving the worker's primary failure.
        pass


def _run_admitted(argv, prompt, *, env, repo_root, artifact_dir, backend,
                  options, parent_session_id, timeout_s, on_spawn, stdout, stderr):
    """Reserve checked memory for this worker until its actual child exits."""
    deadline = time.monotonic() + timeout_s

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise AgentFailed("Workflow agent deadline exhausted during admission or execution")
        return budget

    sid = parent_session_id if parent_session_id is not None else (
        os.environ.get("COORDINATOR_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
    )
    if not isinstance(sid, str) or not sid.strip():
        raise AgentFailed("Workflow agent admission requires the real parent session ID")
    peak_mb = admission_post.valid_peak_mb(options.get("declaredPeakMb", DEFAULT_AGENT_PEAK_GIB * 1024))
    if not peak_mb:
        raise AgentFailed("declaredPeakMb must be a positive finite memory declaration")
    stable_key = f"{backend}-workflow"
    # Parallel broker threads have the same supervisor PID. A shared key would
    # let one sibling END release another's reservation, so reserve uniquely.
    key = f"{stable_key}-{uuid.uuid4().hex[:16]}"
    supervisor_pid = os.getpid()
    try:
        host = capacity._load_box_memory_host()
        measured = host.read_recorded_peak(host.admission_file(), stable_key)
        if measured is not None and (not math.isfinite(measured) or measured <= 0):
            raise ValueError("invalid recorded Workflow peak")
        default_peak = max(peak_mb / 1024, measured or 0)
        admitted = capacity.admit_before_spawn(
            key, default_peak, host=host, register_pid=supervisor_pid, session_id=sid,
            wait_kw={"timeout_s": min(remaining(), slot_wait.SLOT_WAIT_TIMEOUT_S)},
        )
    except Exception as exc:
        raise AgentFailed(f"Workflow memory admission unavailable: {type(exc).__name__}: {exc}") from exc
    if not admitted.admitted:
        _cleanup(capacity.release_spawn, key, supervisor_pid, host=host)
        raise AgentFailed(f"Workflow memory admission refused: {admitted.reason}")
    proc = None
    try:
        remaining()
        actual_peak_mb = admitted.peak_gib * 1024
        if actual_peak_mb > slot_wait.SLOT_THRESHOLD_MB:
            if not slot_grant.check_slot_grant(sid, key, peak_mb=actual_peak_mb)[0]:
                granted = slot_wait.request_and_wait(
                    sid, key, actual_peak_mb, timeout_s=min(remaining(), slot_wait.SLOT_WAIT_TIMEOUT_S)
                )
                if not granted.get("admitted"):
                    raise AgentFailed(f"Workflow slot admission refused: {granted.get('reason', 'unknown')}")
            # A grant can expire or cover less than the recorded memory peak.
            if not slot_grant.check_slot_grant(sid, key, peak_mb=actual_peak_mb)[0]:
                raise AgentFailed("Workflow requires a live Group EM grant covering its recorded peak")
        (artifact_dir / "admission.json").write_text(json.dumps({
            "session_id": sid, "run": key, "stable_key": stable_key,
            "declared_peak_mb": actual_peak_mb, "supervisor_pid": supervisor_pid,
        }), encoding="utf-8")
        env.pop("COORDINATOR_SESSION_ID", None)
        env["COORDINATOR_PARENT_SESSION_ID"] = sid
        remaining()  # A late grant must never start work after its broker deadline.
        proc = subprocess.Popen(
            argv, cwd=repo_root, env=env, stdin=subprocess.PIPE,
            stdout=stdout, stderr=stderr, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        posted = admission_post.post("START", sid, pids=[proc.pid], run=key,
                                     declared_peak_mb=actual_peak_mb)
        if not posted.get("delivered"):
            raise AgentFailed(f"Workflow START admission delivery failed: {posted.get('reason', 'unknown')}")
        if on_spawn:
            on_spawn(proc.pid)
        proc.communicate(prompt.encode("utf-8"), timeout=remaining())
        return proc
    except BaseException:
        if proc is not None:
            _cleanup(proc.kill)
            _cleanup(proc.communicate)
        raise
    finally:
        if proc is not None:
            _cleanup(admission_post.post, "END", sid, pids=[proc.pid], run=key)
        _cleanup(capacity.release_spawn, key, supervisor_pid, host=host)


def _persona(agent_type: str | None) -> str:
    if not agent_type or agent_type == "general-purpose":
        return ""
    if not isinstance(agent_type, str) or not re.fullmatch(r"coordinator:[a-z0-9][a-z0-9-]*", agent_type):
        raise AgentFailed(f"unsupported agentType: {agent_type!r}")
    from coordinator_core.content_root import read_content_root

    root_value = read_content_root()
    if not root_value:
        raise AgentFailed(f"content root unresolved for {agent_type}")
    root = Path(root_value)
    basename = agent_type.split(":", 1)[1] + ".md"
    for path in (root / "coordinator" / "agents" / basename, root / "agents" / basename):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise AgentFailed(f"agent definition missing: {agent_type}")


def run_agent(
    prompt: str,
    options: dict[str, Any] | None,
    *,
    repo_root: Path,
    artifact_dir: Path,
    backend: str = "codex",
    timeout_s: float = 3600,
    on_spawn: Callable[[int], None] | None = None,
    parent_session_id: str | None = None,
) -> Any:
    """Return the schema-validated agent value; persist stdout/stderr and result.

    The caller gives each parallel agent its own artifact directory. This call
    blocks only its worker thread. Failure raises AgentFailed; it never becomes
    a success-shaped value that a Workflow could mistake for completed work.
    """
    options = {} if options is None else options
    if not isinstance(prompt, str) or not isinstance(options, dict):
        raise AgentFailed("agent requires a string prompt and object options")
    if timeout_s <= 0 or timeout_s > 86400:
        raise AgentFailed("agent timeout must be within (0, 86400] seconds")
    repo_root = Path(repo_root).resolve()
    if not repo_root.is_dir():
        raise AgentFailed(f"agent cwd does not exist: {repo_root}")
    schema = options.get("schema")
    if schema is not None:
        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except (jsonschema.SchemaError, TypeError, AttributeError) as exc:
            raise AgentFailed(f"invalid agent schema: {exc}") from exc
    persona = _persona(options.get("agentType"))
    full_prompt = (f"Agent definition ({options['agentType']}):\n{persona}\n\n" if persona else "") + prompt
    if os.name == 'nt':
        full_prompt = (
            'Native execution platform: Windows. Run supplied commands with PowerShell. '
            'For a quoted executable use the PowerShell call operator &, followed by the quoted path. '
            'Do not wrap a command in cmd.exe /c, use POSIX parameter expansion, or add literal backslash-escaped quotes. '
            'Use Python or native PowerShell for file operations. Preserve all operation arguments and JSON payloads.\n\n'
        ) + full_prompt
    native_schema = _codex_schema(schema) if schema is not None else None
    if backend == "codex" and schema is not None and native_schema is None:
        full_prompt += (
            "\n\nYour final response must be exactly one raw JSON value, with no Markdown fences or commentary. "
            "It must satisfy this exact JSON Schema. Preserve dynamic mapping entries and optional/nullable "
            "field semantics; do not replace argument objects with empty objects:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "prompt.txt").write_text(full_prompt, encoding="utf-8")
    env = _environment()
    env['COORDINATOR_AGENT_PROVIDER'] = backend
    last = artifact_dir / "last.txt"
    # A reused directory must not let stale successful output mask a failure.
    last.unlink(missing_ok=True)
    if backend == "codex":
        preflight_auth(env=env, requested=False)
        argv = [*resolve_codex(env=env), "exec", "--json", "--sandbox", "workspace-write",
                "--ephemeral", "-o", str(last)]
        if schema is not None:
            schema_path = artifact_dir / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            if native_schema is not None:
                argv += ["--output-schema", str(schema_path)]
        model = options.get("codexModel")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise AgentFailed("codexModel must be a non-empty native model ID")
            argv += ["--model", model]
        argv += ["-"]
    elif backend == "claude":
        executable = shutil.which("claude", path=env.get("PATH"))
        if not executable:
            raise AgentFailed("claude executable unavailable")
        argv = [executable, "-p", "--output-format", "json"]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]
        if options.get("model"):
            argv += ["--model", str(options["model"])]
    else:
        raise AgentFailed(f"unknown agent backend: {backend}")
    try:
        with (artifact_dir / "stdout.jsonl").open("wb") as out, (artifact_dir / "stderr.txt").open("wb") as err:
            proc = _run_admitted(
                argv, full_prompt, env=env, repo_root=repo_root, artifact_dir=artifact_dir,
                backend=backend, options=options, parent_session_id=parent_session_id,
                timeout_s=timeout_s, on_spawn=on_spawn, stdout=out, stderr=err,
            )
    except subprocess.TimeoutExpired as exc:
        raise AgentFailed(f"{backend} agent timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise AgentFailed(f"{backend} spawn failed: {exc}") from exc
    (artifact_dir / "process.json").write_text(json.dumps({"pid": proc.pid, "exit_code": proc.returncode,
        "backend": backend, "label": options.get("label")}), encoding="utf-8")
    if proc.returncode != 0:
        raise AgentFailed(f"{backend} agent exited {proc.returncode}; see {artifact_dir / 'stderr.txt'}")
    try:
        if backend == "claude":
            envelope = json.loads((artifact_dir / "stdout.jsonl").read_text(encoding="utf-8"))
            if envelope.get("is_error"):
                raise AgentFailed(f"claude reported an error: {envelope.get('result', '')}")
            value = envelope.get("structured_output") if schema is not None else envelope.get("result")
            if schema is not None and value is None:
                value = json.loads(envelope["result"])
        else:
            text = last.read_text(encoding="utf-8")
            value = json.loads(text) if schema is not None else text
        if schema is not None:
            jsonschema.validate(value, schema)
    except (OSError, ValueError, KeyError, jsonschema.ValidationError) as exc:
        raise AgentFailed(f"{backend} returned no schema-valid result: {exc}") from exc
    (artifact_dir / "result.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value
