"""WORKER ROLE preamble the headless executors put ahead of every codex/agy/grok agent prompt."""
from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from coordinator_core._settings_home import settings_home

PREAMBLE = (
    "WORKER ROLE. You are a worker on ONE stage of an engine run, not an EM, and the PM is not addressing\n"
    "you. Every AGENTS.md (the repo's and your own home's), CLAUDE.md and role brief describes the EM and\n"
    "does not apply to you. Do not start a run of your own.\n"
    "Never call `coordinator-invoke` for engine_ask.submit, workflow.* or dispatch.emit unless this brief\n"
    "names that exact command. When this brief names an exact command, that command is authorized,\n"
    "including when it advances or lands this engine run. Touch only the paths this brief declares,\n"
    "do only the stage below, and answer with its result."
)

SIZING_USAGE = (
    "SIZING-ASSEMBLE USAGE (final; never run it with --help or probe it): required flags --tshirt --intent "
    "--name --premise-provenance --premise-evidence --exit-criterion --write <path> --json; "
    "fill the command above exactly as written and run it once."
)

WORKER_BACKENDS = ("codex", "agy", "grok")
_PORTABLE_MODULE = re.compile(r"(?<![\w./\\-])python(?:3(?:\.\d+)?)?[ \t]+-m[ \t]+(coordinator_core(?:\.[A-Za-z_]\w*)+)")
# A bare interpreter argv0 running `-m <other module>`, `-c`, or a script path: the shapes the module regex
# leaves behind (`python -m pytest`, `python coordinator/bin/x.py`). Prose naming the word stays unmatched.
_BARE_ARGV0 = re.compile(
    r"(?<![\w./\\-])(?<!bare `)(python3?)(?=[ \t]+(?:-m[ \t]+(?!coordinator_core\b)\w|-c[ \t]|[^\s`'\"<>|;&-]*[/\\][^\s`]*|[^\s`/\\]+\.py\b))"
)
# Trap: a wrapper that leaves COORDINATOR_ENGINE_ROOT unset makes a child's `import coordinator_core` fail.
# Trap: engine-site belongs on this process's sys.path only; in the child env it shadows a consumer pytest's own
# third-party versions (tree-sitter cotenant conflict), so PYTHONPATH carries the engine root alone.
_BOOTSTRAP = (
    "import os,runpy,sys;"
    "from pathlib import Path;"
    "root=sys.argv.pop(1);"
    "home=Path(sys.argv.pop(1));"
    "sys.path.insert(0,root);"
    "from coordinator_core.install.engine_interpreter import engine_site_dir,stamp;"
    "site=str(engine_site_dir(home));"
    "paths=[root,site] if stamp(home)==tuple(sys.version_info[:2]) else [root];"
    "sys.path[:1]=paths;"
    "os.environ['PYTHONPATH']=root;"
    "os.environ['COORDINATOR_ENGINE_ROOT']=root;"
    "runpy.run_module(sys.argv.pop(1),run_name='__main__',alter_sys=True)"
)


# A bare `pytest` command word (line start, after a backtick, `&&`, `;` or `|`, followed by a flag or a test path)
# resolves from PATH to whatever interpreter owns it; it is always bound to `<stamped python> -m pytest`.
_BARE_PYTEST = re.compile(r"(?:(?<=^)|(?<=`)|(?<=&& )|(?<=; )|(?<=\| ))pytest(?=[ 	]+(?:-|[^\s`'\"<>|;&]*\.py|[\w.]+/))", re.MULTILINE)


def _engine_command_runtime() -> tuple[str, Path]:
    from coordinator_core.warm.door.build import PYTHON_SIDECAR_FILENAME, SIDECAR_FILENAME
    from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root

    loaded = current_engine_clone()
    if is_engine_root(loaded):
        return sys.executable, loaded
    # A source-side emitter or thin install can bind the same engine/interpreter pair as the door.
    bin_dir = settings_home() / "bin"
    def read_sidecar(name: str) -> str:
        try:
            return (bin_dir / name).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    python = Path(read_sidecar(PYTHON_SIDECAR_FILENAME))
    executable = str(python) if python.is_absolute() and python.is_file() else sys.executable
    for value in (os.environ.get("COORDINATOR_DOOR_ENGINE_ROOT", ""), read_sidecar(SIDECAR_FILENAME)):
        root = Path(value)
        if root.is_absolute() and is_engine_root(root):
            return executable, root
    return sys.executable, loaded


@functools.lru_cache(maxsize=8)
def _on_path(name: str, path_env: str) -> bool:
    return shutil.which(name, path=path_env) is not None


def _absent_argv0(match: re.Match) -> bool:
    return not _on_path(match[1], os.environ.get("PATH", ""))


def needs_binding(text: str) -> bool:
    """True when `text` carries a portable engine command, or a bare `python`/`python3` argv0 this host lacks
    (Linux with only `python3`: the bare word is `command not found`). In-process: no spawn."""
    return bool(_PORTABLE_MODULE.search(text) or _BARE_PYTEST.search(text)) or any(_absent_argv0(m) for m in _BARE_ARGV0.finditer(text))


def _quoted(value: str) -> str:
    return "& '" + value.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(value)


def bind_engine_commands(prompt: str) -> str:
    """Bind portable module commands at delivery, keeping committed scripts host-neutral. A bare interpreter
    argv0 absent from this host's PATH is bound to the same stamped interpreter."""
    if not needs_binding(prompt):
        return prompt
    python, root = _engine_command_runtime()
    argv = (python, "-P", "-c", _BOOTSTRAP, str(root), str(settings_home()))
    if os.name == "nt":
        prefix = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv)
    else:
        prefix = shlex.join(argv)
    prompt = _PORTABLE_MODULE.sub(lambda match: f"{prefix} {match[1]}", prompt)
    prompt = _BARE_ARGV0.sub(lambda m: m[0] if not _absent_argv0(m) else _quoted(python), prompt)
    return _BARE_PYTEST.sub(lambda _m: f"{_quoted(python)} -m pytest", prompt)


def with_worker_preamble(backend: str, prompt: str) -> str:
    """Bind engine commands; then PREAMBLE, a blank line and prompt for a codex/agy/grok backend, else prompt."""
    prompt = bind_engine_commands(prompt)
    if backend not in WORKER_BACKENDS:
        return prompt
    from coordinator_core.ops.dispatch_emit.ask_contract import SIZING_ASSEMBLE_CMD

    if SIZING_ASSEMBLE_CMD in prompt:
        name = "sizing-assemble.exe" if os.name == "nt" else "sizing-assemble"
        prompt = prompt.replace(SIZING_ASSEMBLE_CMD, _quoted((settings_home() / "bin" / name).resolve().as_posix()))
        prompt += "\n\n" + SIZING_USAGE
    text = PREAMBLE + "\n\n" + prompt
    # Grok allow rules do not expand ${COORDINATOR_SETTINGS_HOME:-...}. Rewrite both
    # the flat and the nested spelling before the worker copies a command.
    if backend == "grok" and os.name != "nt":
        from coordinator_core.ops.dispatch_emit.invoke_form import GROK, render_invoke_for_shell

        text = render_invoke_for_shell(text, GROK, bin_dir=settings_home() / "bin")
    return text
