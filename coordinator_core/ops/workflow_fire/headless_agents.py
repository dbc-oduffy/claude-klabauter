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

from coordinator_core import tenant_priority
from coordinator_core.codex_runner.codex_spawn_env import shape_codex_child, writeback_codex_auth
from coordinator_core.codex_runner.resolve import codex_family_stage_argv, preflight_auth, resolve_codex
from coordinator_core.codex_runner.sandbox_preflight import SandboxSetupRefused
from coordinator_core.codex_runner.review import _unattended_env
from coordinator_core.bash_guards import _heavy_command_class as capacity
from coordinator_core.group_em import admission_post, slot_grant, slot_wait
from coordinator_core.ops.workflow_fire.worker_preamble import with_worker_preamble

DEFAULT_AGENT_PEAK_GIB = 1.0


class AgentFailed(RuntimeError):
    """The worker failed, timed out, or returned data outside its schema."""


class AgentPolicyDenied(AgentFailed):
    """The backend's native harness refused a tool call; `denial` is {tool, command, decision}, verbatim."""

    def __init__(self, denial: dict[str, str]):
        self.denial = denial
        super().__init__(f"policy denial: tool={denial['tool']} command={denial['command']} "
                         f"decision={denial['decision']}")


_DENIAL = re.compile(r"permission required: (?P<tool>\w+) `(?P<command>.*?)`? (?P<decision>was not run, as a headless "
                     r"run cannot ask for approval)", re.DOTALL)


def policy_denial(artifact_dir: Path) -> dict[str, str] | None:
    """The last native permission denial in the agent's stderr.txt or stdout.jsonl, else None."""
    found = None
    for name in ("stderr.txt", "stdout.jsonl"):
        try:
            text = (artifact_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _DENIAL.finditer(text):
            found = {"tool": m["tool"], "command": m["command"], "decision": m["decision"]}
        if found:
            return found
    return None


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


def _admits_null(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    declared = node.get("type")
    if declared == "null" or (isinstance(declared, list) and "null" in declared):
        return True
    return any(_admits_null(alt) for alt in node.get("anyOf", ()) if isinstance(node.get("anyOf"), list))


def unwrap_rpc_envelope(value: Any, schema: Any) -> Any:
    """Return the ``result`` of a JSON-RPC envelope Codex wrapped around its reply.

    Applies only when the schema itself has no ``jsonrpc`` member and the
    envelope carries nothing beyond jsonrpc/id/result (and a string note).
    """
    if not (isinstance(value, dict) and value.get("jsonrpc") == "2.0" and isinstance(schema, dict)):
        return value
    if "jsonrpc" in (schema.get("properties") or {}):
        return value
    keys = set(value)
    if keys - {"jsonrpc", "id", "result", "note"} or "result" not in keys or "id" not in keys:
        return value
    if "note" in value and not isinstance(value["note"], str):
        return value
    return value["result"]


def fill_omitted_nullable(value: Any, schema: Any) -> Any:
    """Return value with omitted required null-admitting properties set to null.

    Codex may omit a nullable member that strict mode forced into ``required``.
    Only that case is normalised; every other violation still fails validation.
    An object carrying none of the schema's members is the wrong shape, not a
    reply with every member omitted, so it is left to fail.
    """
    if isinstance(value, dict) and isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict) and (not properties or set(value) & set(properties)):
            value = dict(value)
            for name, sub in properties.items():
                if name in value:
                    value[name] = fill_omitted_nullable(value[name], sub)
                elif name in schema.get("required", ()) and _admits_null(sub):
                    value[name] = None
    elif isinstance(value, list) and isinstance(schema, dict) and isinstance(schema.get("items"), dict):
        value = [fill_omitted_nullable(item, schema["items"]) for item in value]
    return value


def _environment() -> dict[str, str]:
    # Do not inherit API-key opt-ins, parent session identity or Claude nesting.
    source = dict(os.environ)
    source.pop("CODEX_RUNNER_ALLOW_API_KEY", None)
    source.pop("OPENAI_API_KEY", None)
    source.pop("CODEX_API_KEY", None)
    env = _unattended_env(source, requested=False)
    # Only the headless-em-launch session holds the EM token; a worker child never does.
    env.pop("COORDINATOR_ALLOW_HEADLESS_EM_TOKEN", None)
    # Reach a live engine, never start one: warmth stays on (an inherited WARM=0
    # is dropped) while both autostart gates are pinned off.
    env.pop('COORDINATOR_WARM', None)
    env['COORDINATOR_WARM_LISTENER_AUTOSTART'] = '0'
    env['COORDINATOR_WARM_SERVER_AUTOSTART'] = '0'
    # Native operation doors validate this stamped build using their normal
    # bootstrap ladder; never silently dispatch back into another live clone.
    env['COORDINATOR_ENGINE_ROOT'] = str(Path(__file__).resolve().parents[3])
    # The native door ignores COORDINATOR_ENGINE_ROOT and ranks its own override above the installed
    # sidecar: unpinned, a worker's ops reach whichever engine the sidecar names, not the host's.
    env['COORDINATOR_DOOR_ENGINE_ROOT'] = env['COORDINATOR_ENGINE_ROOT']
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
            raise AgentFailed(f"{backend} agent timed out after {timeout_s}s (deadline exhausted during admission or execution)")
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
            **tenant_priority.popen_kwargs(argv),
        )
        posted = admission_post.post("START", sid, pids=[proc.pid], run=key,
                                     declared_peak_mb=actual_peak_mb)
        admission_post.record_undelivered(artifact_dir, {"phase": "START", **posted})
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


_REFUSAL_KEYS = frozenset({"refused", "reason"})


def _refused_submit_outputs(stdout_path: Path) -> list[str]:
    """Output text of every refused ``engine_ask.submit`` command in a codex transcript."""
    refused: list[str] = []
    try:
        lines = stdout_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return refused
    for line in lines:
        try:
            item = json.loads(line).get("item")
        except (ValueError, AttributeError):
            continue
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        output = item.get("aggregated_output")
        if ("engine_ask.submit" in str(item.get("command", ""))
                and isinstance(output, str) and re.search(r'"refused"\s*:', output)):
            refused.append(output.strip())
    return refused


def _is_refusal_only(value: Any, refused: list[str]) -> bool:
    """True when the worker's final value is nothing but a refused submit's own output."""
    if isinstance(value, str):
        text = value.strip()
        if not text or text in refused:
            return True
        try:
            value = json.loads(text)
        except ValueError:
            return False
    if isinstance(value, dict):
        inner = value.get("result") if isinstance(value.get("result"), dict) else value
        return "refused" in inner and set(inner) <= _REFUSAL_KEYS and set(value) <= _REFUSAL_KEYS | {"jsonrpc", "id", "result"}
    return False


def _persona(agent_type: str | None) -> str:
    if not agent_type or agent_type == "general-purpose":
        return ""
    if not isinstance(agent_type, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*:[a-z0-9][a-z0-9-]*", agent_type):
        raise AgentFailed(f"unsupported agentType: {agent_type!r}")
    if not agent_type.startswith("coordinator:"):
        # A domain persona (e.g. game-dev:staff-game-dev) is an installed plugin agent, not a content-root one.
        from coordinator_core.hooks.block_unenumerated_agent_type import plugin_agent_model

        _, found = plugin_agent_model(agent_type)
        if found is not None and found.is_file():
            return found.read_text(encoding="utf-8")
        raise AgentFailed(f"agent definition missing: {agent_type}")
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


_REASK_REPLY_LIMIT = 20000


def _reask_unparsable(exc, last, prompt, options, schema, repo_root, artifact_dir, backend, timeout_s,
                      on_spawn, parent_session_id) -> Any:
    """One bounded repair re-ask for a reply that is not JSON or not schema-valid; a second failure raises, never loops."""
    try:
        text = last.read_text(encoding="utf-8")
    except OSError:
        text = ""
    failure = AgentFailed(f"{backend} returned no schema-valid result: {exc}")
    if not text or len(text) > _REASK_REPLY_LIMIT:
        raise failure from exc
    if isinstance(exc, jsonschema.ValidationError):
        repair = (
            "The following reply is valid JSON but fails the required JSON Schema. Do not run any tools or "
            "repeat the operation. Return the complete corrected reply, conforming to the schema, as one raw "
            "JSON value; keep every correct member and add or fix only what the validation error names.\n"
            + json.dumps({"validation_error": exc.message[:400],
                          "path": "/".join(str(p) for p in exc.absolute_path) or "(root)",
                          "reply": text}, ensure_ascii=False)
        )
    else:
        repair = (
            "Repair JSON syntax in the following data. Do not run any tools or repeat the operation. "
            "Return the complete original reply with only JSON punctuation or whitespace repaired.\n"
            + json.dumps({"diagnostic": str(exc)[:400], "reply": text}, ensure_ascii=False)
        )
    try:
        return run_agent(repair, {k: v for k, v in options.items() if k != "agentType"}, repo_root=repo_root,
                         artifact_dir=Path(artifact_dir) / "reply-reask", backend=backend, timeout_s=timeout_s,
                         on_spawn=on_spawn, parent_session_id=parent_session_id, _reask=True)
    except AgentFailed as second:
        raise AgentFailed(f"{failure} (one repair re-ask also failed: {second})") from exc


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
    _reask: bool = False,
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
    full_prompt = with_worker_preamble(backend, full_prompt)
    if backend == "grok" and os.name != "nt":
        from coordinator_core._settings_home import settings_home
        from coordinator_core.ops.dispatch_emit.invoke_form import GROK, render_invoke_for_shell

        # Grok allow rules do not expand ${COORDINATOR_SETTINGS_HOME:-$HOME/...}.
        full_prompt = render_invoke_for_shell(full_prompt, GROK, bin_dir=settings_home() / "bin")
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
        argv = codex_family_stage_argv(resolve_codex(env=env), "--ephemeral", "-o", str(last))
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
        try:
            shape_codex_child(env, writable_root=repo_root)
        except SandboxSetupRefused as exc:
            raise AgentFailed(str(exc)) from exc
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
    finally:
        if backend == "codex":
            writeback_codex_auth(env)
    (artifact_dir / "process.json").write_text(json.dumps({"pid": proc.pid, "exit_code": proc.returncode,
        "backend": backend, "label": options.get("label")}), encoding="utf-8")
    denial = policy_denial(artifact_dir) if backend != "claude" else None
    if denial and proc.returncode != 0:
        raise AgentPolicyDenied(denial)
    if proc.returncode != 0:
        raise AgentFailed(
            f"{backend} agent exited {proc.returncode}; see {artifact_dir / 'stderr.txt'} "
            f"and {artifact_dir / 'stdout.jsonl'}"
        )
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
            value = fill_omitted_nullable(unwrap_rpc_envelope(value, schema), schema)
            jsonschema.validate(value, schema)
    except (OSError, ValueError, KeyError, jsonschema.ValidationError) as exc:
        if (isinstance(exc, (json.JSONDecodeError, jsonschema.ValidationError))
                and backend == "codex" and schema is not None and not _reask):
            return _reask_unparsable(exc, last, prompt, options, schema, repo_root, artifact_dir, backend, timeout_s,
                                     on_spawn, parent_session_id)
        if denial:
            raise AgentPolicyDenied(denial) from exc
        raise AgentFailed(f"{backend} returned no schema-valid result: {exc}") from exc
    if backend != "claude":
        refused = _refused_submit_outputs(artifact_dir / "stdout.jsonl")
        if refused and _is_refusal_only(value, refused):
            raise AgentFailed(f"{backend} agent's only output is a refused engine_ask.submit: {refused[-1][-300:]}")
    (artifact_dir / "result.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value
