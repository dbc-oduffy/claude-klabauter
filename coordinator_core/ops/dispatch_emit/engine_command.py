"""Render an engine module command for a runtime prompt or a portable script."""
import shlex
from pathlib import Path
from typing import Optional

OVERLAY_VERIFY_MODULE = "coordinator_core.ops.dispatch_emit.clean_overlay_verify"


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def engine_module_command(module: str, repo_root: str, args: str = "", *, portable: bool = False,
                          shell: Optional[str] = None) -> str:
    """`<engine interpreter> -m <module> <args>` for a command an agent runs to reach `coordinator_core`.
    The interpreter is the repo's resolvable pin, else this process's own absolute `sys.executable`; never a
    bare `python`, which on a host whose PATH python lacks `coordinator_core` fails before the module runs.
    `PYTHONPATH` names the engine checkout so the module imports from a consumer repo cwd.
    Trap: both carry host paths; `portable` drops them for text inside a committed emitted script, which carries
    no host path (`test_emitted_script_carries_no_host_path`), leaving a bare `python` that needs `coordinator_core` importable."""
    from coordinator_core.ops.dispatch_emit.clean_overlay_verify import engine_python_command

    if portable and shell is None:
        return f"python -m {module}" + (f" {args}" if args else "")
    engine_root = Path(__file__).resolve().parents[3].as_posix()
    if shell is not None:
        if portable:
            # Trap: an agent shell often inherits no COORDINATOR_ENGINE_ROOT and its cwd (a consumer repo)
            # holds no coordinator_core, so with no fallback runpy dies ModuleNotFoundError and the leg
            # zero-runs. The fallback reads machine-local repos.claude_klabauter by one `machine-local get`,
            # spent only on that path. Never repos.claude_klabauter: the resolved root is the published mirror,
            # never an unpublished claude-klabauter tree (DoE engine-root rule). No host path is baked into the text.
            bootstrap = (
                "import os,runpy,sys,shutil,subprocess as s,importlib.util as u;"
                "root=os.environ.get('COORDINATOR_ENGINE_ROOT');module=sys.argv.pop(1);"
                "ml=not root and not u.find_spec('coordinator_core') and shutil.which('machine-local');"
                "root=root or ml and next((v for v in [s.run([ml,'get','repos.claude_klabauter'],"
                "capture_output=True,text=True).stdout.strip()] "
                "if v and os.path.isdir(os.path.join(v,'coordinator_core'))),None);"
                "root or u.find_spec('coordinator_core') or sys.exit('coordinator engine root unresolved for '+module+"
                "': COORDINATOR_ENGINE_ROOT is unset and machine-local has no repos.claude_klabauter. "
                "Fetch claude-klabauter, then: machine-local set repos.claude_klabauter <its checkout>');"
                "path=os.path.join(root,*module.split('.'))+'.py' if root else None;"
                "root and (os.path.isabs(root) and os.path.isfile(path) or "
                "sys.exit('Invalid COORDINATOR_ENGINE_ROOT for '+module+': '+root));"
                "root and (os.environ.update(PYTHONPATH=root),sys.path.insert(0,root));"
                "runpy.run_module(module,run_name='__main__',alter_sys=True)"
            )
        else:
            bootstrap = (
                "import os,runpy,sys;root=sys.argv.pop(1);"
                "os.environ['PYTHONPATH']=root;sys.path.insert(0,root);"
                "runpy.run_module(sys.argv.pop(1),run_name='__main__',alter_sys=True)"
            )
        python = "python" if portable else engine_python_command(Path(repo_root))
        if shell == "powershell":
            command = shlex.split(python)
            executable = "& " + " ".join(powershell_quote(part) for part in command)
            quote = powershell_quote
        else:
            executable = python
            quote = shlex.quote
        root_arg = "" if portable else f"{quote(engine_root)} "
        call = f"{executable} -c {quote(bootstrap)} {root_arg}{quote(module)}"
        return call + (f" {args}" if args else "")
    return (
        f'PYTHONPATH="{engine_root}" {engine_python_command(Path(repo_root))} -m {module}'
        + (f" {args}" if args else "")
    )
