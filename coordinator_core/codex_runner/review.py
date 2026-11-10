"""codex_runner.review -- read-only codex review of one staged artifact.

The artifact is copied into `<run_dir>/input/` and codex runs with cwd = run_dir under
`--sandbox read-only`. The sandbox does not stop reads, so confinement is detection after
the fact: `audit_confinement` flags absolute or `..`-escaping path arguments in codex's
command events and the result carries `confinement: {breached, paths, basis}`.
"""
from __future__ import annotations

from coordinator_core.codex_runner.identity_env import provider_identity_env

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

import jsonschema

from coordinator_core.codex_runner.contract import (
    FALLBACK_REVIEWER,
    STATUS_CONFINEMENT_BREACH,
    STATUS_FAILED,
    STATUS_LIMIT_EXHAUSTED,
    STATUS_OK,
    OnSpawn,
    RunRequest,
    RunResult,
    findings_schema_path,
)
from coordinator_core.codex_runner.resolve import (
    API_KEY_ENV,
    ApiKeyRefused,
    CodexUnavailable,
    allow_api_key,
    preflight_auth,
    resolve_codex,
)

CODEX_TIMEOUT_S = 900
INPUT_DIRNAME = "input"
EVENTS_FILE = "events.jsonl"
STDERR_FILE = "stderr.txt"
LAST_FILE = "last.json"

CONFINEMENT_BASIS = "post-run audit of command events; detection, not an OS read jail"

# Substrings of codex's usage-limit errors (fixture basis: README "limit_exhausted.jsonl").
_LIMIT_MARKERS = (
    "hit your usage limit",
    "rate_limit_reached",
    "usage_limit_reached",
    "credits_depleted",
)

_ENV_ALLOW = ("PATH", "HOME", "USERPROFILE", "CODEX_HOME", "LANG")
# A Windows child missing SystemDrive/windir expands %SystemDrive% relative to its cwd and writes junk there.
# USERPROFILE rides in the base allowlist. Shared by the agy and grok spawn envs.
_ENV_ALLOW_WINDOWS = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC")

_ABS_RE = re.compile(r"^(?:/|~|[A-Za-z]:[\\/]|\\\\)")
_SHELL_FLAG_RE = re.compile(r"^-[A-Za-z]*c$")
_HARMLESS = frozenset({"/dev/null"})


def _build_env(
    environ: Optional[dict] = None, windows: Optional[bool] = None, requested: bool = False, role: str = "reviewer"
) -> dict[str, str]:
    """Allowlisted child env; API-key vars pass only under the allow_api_key flag (env or `requested`)."""
    environ = os.environ if environ is None else environ
    windows = sys.platform.startswith("win") if windows is None else windows
    keys = _ENV_ALLOW + (_ENV_ALLOW_WINDOWS if windows else ())
    if allow_api_key(environ, requested):
        keys = keys + API_KEY_ENV
    return provider_identity_env({k: environ[k] for k in keys if environ.get(k)}, environ, provider="codex", role=role)


def _unattended_env(*args, **kwargs) -> dict[str, str]:
    """`_build_env` plus GIT_TERMINAL_PROMPT=0: unattended agent shells must fail instead of prompting."""
    child = _build_env(*args, **kwargs)
    child["GIT_TERMINAL_PROMPT"] = "0"
    return child


def _stage_artifact(artifact: str, run_dir: Path) -> Path:
    src = Path(artifact)
    if not src.exists():
        raise FileNotFoundError(f"artifact not found: {artifact}")
    dest_dir = run_dir / INPUT_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)
    return dest


def _prompt(staged: Path, run_dir: Path) -> str:
    rel = staged.relative_to(run_dir).as_posix()
    return (
        f"Review the artifact at ./{rel} (relative to the current directory). "
        "Read only that path; do not read, list or execute anything outside it. "
        "Report concrete defects as findings and give an overall verdict "
        "(OK, WARN or FAIL), as JSON matching the supplied output schema."
    )


def _events(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _tokens(command: str) -> list[str]:
    """Split a shell command line into words and operator tokens (`;`, `|`, `&&`, ...)."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return command.split()


def _words(command: str, depth: int = 0) -> Iterable[str]:
    """Yield every non-executable word of `command`, recursing into `sh -c '<script>'` bodies."""
    toks = _tokens(command)
    at_exe = True
    i = 0
    while i < len(toks):
        tok = toks[i]
        if re.fullmatch(r"[;|&()<>]+", tok):
            at_exe = tok not in ("<", ">", ">>", "<<")
            i += 1
            continue
        if at_exe:
            at_exe = False
            nxt = toks[i + 1 : i + 3]
            # `bash -lc '<script>'`: audit the script body, not the script as one word.
            if depth < 3 and nxt and _SHELL_FLAG_RE.match(nxt[0]) and len(nxt) > 1:
                yield from _words(nxt[1], depth + 1)
                return
        else:
            yield tok
        i += 1


def _path_of(word: str) -> Optional[str]:
    """The path inside a word (`--flag=/x` included), or None when it names none."""
    cand = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
    if _ABS_RE.match(cand) or ".." in re.split(r"[\\/]", cand):
        return cand
    return None


def _inside(path: str, root: Path) -> bool:
    if path in _HARMLESS:
        return True
    if path.startswith("~"):
        return False
    p = Path(path)
    p = p if p.is_absolute() else root / p
    resolved = Path(os.path.realpath(p))
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True


def audit_confinement(events: list[dict[str, Any]], run_dir: Path) -> dict[str, Any]:
    """Flag path arguments of codex command events that resolve outside `run_dir`."""
    root = Path(os.path.realpath(run_dir))
    seen: list[str] = []
    for ev in events:
        item = ev.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        for word in _words(command):
            path = _path_of(word)
            if path is not None and not _inside(path, root) and path not in seen:
                seen.append(path)
    return {"breached": bool(seen), "paths": seen, "basis": CONFINEMENT_BASIS}


def detect_limit(events: list[dict[str, Any]], stderr: str = "") -> Optional[str]:
    """The usage-limit message when codex's error/turn.failed events (or stderr) carry one."""
    texts: list[str] = []
    for ev in events:
        if ev.get("type") == "error":
            texts.append(str(ev.get("message", "")))
        elif ev.get("type") == "turn.failed":
            err = ev.get("error")
            texts.append(str(err.get("message", "")) if isinstance(err, dict) else str(err or ""))
    texts.append(stderr)
    for text in texts:
        low = text.lower()
        if any(m in low for m in _LIMIT_MARKERS):
            return text.strip() or "usage limit reached"
    return None


def _usage(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for ev in reversed(events):
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            return ev["usage"]
    return None


def _tail(path: Path, limit: int = 600) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
    except OSError:
        return ""


def _failed(run_id: str, error: str, **kw: Any) -> RunResult:
    return RunResult(run_id=run_id, status=STATUS_FAILED, error=error, **kw)


def run(req: RunRequest, run_dir: Path, on_spawn: OnSpawn) -> RunResult:
    """Run one read-only codex review; calls on_spawn(codex_pid) once right after Popen."""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    try:
        preflight_auth(requested=req.allow_api_key)
    except ApiKeyRefused as exc:
        return _failed(run_id, f"refused: {exc}")
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        staged = _stage_artifact(req.artifact or "", run_dir)
        argv_prefix = resolve_codex()
    except (OSError, CodexUnavailable) as exc:
        return _failed(run_id, f"{type(exc).__name__}: {exc}")

    events_path = run_dir / EVENTS_FILE
    stderr_path = run_dir / STDERR_FILE
    last_path = run_dir / LAST_FILE
    last_path.unlink(missing_ok=True)
    argv = argv_prefix + [
        "exec", "--json", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
        "--output-schema", str(findings_schema_path()),
        "-o", str(last_path),
        _prompt(staged, run_dir),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
    with open(events_path, "wb") as out, open(stderr_path, "wb") as err:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(run_dir), env=_unattended_env(requested=req.allow_api_key), shell=False,
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=flags,
            )
        except OSError as exc:
            return _failed(run_id, f"codex spawn failed: {exc}")
        try:
            on_spawn(proc.pid)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        try:
            rc = proc.wait(timeout=CODEX_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return _failed(run_id, f"codex timed out after {CODEX_TIMEOUT_S}s", codex_pid=proc.pid)

    events = _events(events_path)
    usage = _usage(events)
    pid = proc.pid

    limit = detect_limit(events, _tail(stderr_path, 4000))
    if limit is not None:
        return RunResult(
            run_id=run_id, status=STATUS_LIMIT_EXHAUSTED, fallback=FALLBACK_REVIEWER,
            codex_pid=pid, usage=usage, error=limit,
        )
    if rc != 0:
        return _failed(run_id, f"codex exited {rc}: {_tail(stderr_path)}", codex_pid=pid, usage=usage)

    try:
        payload = json.loads(last_path.read_text(encoding="utf-8"))
        schema = json.loads(findings_schema_path().read_text(encoding="utf-8"))
        jsonschema.validate(payload, schema)
    except (OSError, ValueError, jsonschema.ValidationError) as exc:
        return _failed(
            run_id, f"no schema-valid findings in {LAST_FILE}: {type(exc).__name__}: {exc}",
            codex_pid=pid, usage=usage,
        )

    confinement = audit_confinement(events, run_dir)
    return RunResult(
        run_id=run_id,
        status=STATUS_CONFINEMENT_BREACH if confinement["breached"] else STATUS_OK,
        findings=payload["findings"], verdict=payload["verdict"],
        confinement=confinement, codex_pid=pid, usage=usage,
    )
