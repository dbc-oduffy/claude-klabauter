"""Manual repro harness for the generated-`.cmd`-forwarder argv re-parse defect.

Backs `state/bug-backlog/2026-08-15-cmd-exe-re-parses-in-all-382-generated-f-e6db655378b5.yaml`.

Every `.cmd` forwarder this repo generates ends in `"%_py%" "%~dp0<entry>" %*`. After cmd.exe
substitutes `%*` it RE-PARSES the resulting line for redirection and command-separator
operators, so an argument carrying `>` `<` `|` `&` that reached the command line unquoted
breaks out of the argument and executes as shell syntax inside the launcher body. The trigger
is narrow enough to look intermittent: `subprocess.list2cmdline` quotes only arguments holding
a space or a quote, so `"a > b"` is safe and `"a>b"` is not.

NOT a pytest module, deliberately. `python_files = ["test_*.py"]` in pyproject.toml matches only
the literal `test_` prefix, so the `repro_` name keeps this out of collection: it is Windows-only,
spawns real `cmd.exe`/`powershell` processes, and on a box averaging 50-70 concurrent LLM sessions
it has no business running in a gated suite. Run it by hand when working the fix:

    python coordinator/bin/tests/repro_cmd_forwarder_argv_reparse.py

NEGATIVE SPEC — this is NOT the caret defect and must not be merged into it. The caret is lost
while cmd.exe parses its own `/c` string, BEFORE the launcher body runs, which is why
`gen-launcher-shim.py`'s `_cmd_raw_cmdline_block` docstring records that the generator cannot
reach it. The corruption probed here fires during the post-`%*`-substitution re-parse INSIDE the
body — a different parse phase, and one the generator does have a lever on.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PY = sys.executable
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REPO = Path(__file__).resolve().parents[3]
ENTRY_SRC = 'import sys\nprint("PAYLOAD " + repr(sys.argv[1:]))\n'

#: Bodies isolating our generator's framing from the bare forwarding line. `minimal` is the
#: control that decides scope: if it leaks identically, the defect is generic cmd.exe `%*`
#: behaviour affecting every forwarder, not a quoting slip in our template.
_CMD_BODIES = {
    "minimal": '@echo off\r\n"{py}" "%~dp0probe-cli.py" %*\r\n',
    "generator-shaped": (
        '@echo off\r\nsetlocal\r\n"{py}" "%~dp0probe-cli.py" %*\r\nexit /b %ERRORLEVEL%\r\n'
    ),
}

#: Left column is the single argument handed to `subprocess.run`; the interesting output is
#: which file it causes to appear in the caller's cwd. The two `%~dp0`/`execute` rows are the
#: artifacts the peer P2 was filed on; `a > b` is the negative control that Python quotes.
_ARG_CASES = {
    "dp0-adjacent": "foo>%~dp0",
    "dp0-stderr": "2>%~dp0",
    "execute-adjacent": "mode>execute",
    "execute-bareword": ">execute",
    "sha-range-gt": "abc>def..ghi",
    "ampersand": "a&b",
    "pipe": "a|b",
    "spaced-so-quoted": "a > b",
}


def _render_probe_tree() -> Path:
    src = Path(tempfile.mkdtemp(prefix="fwd_repro_src_"))
    (src / "probe-cli.py").write_text(ENTRY_SRC, encoding="utf-8")
    for name, body in _CMD_BODIES.items():
        (src / f"{name}.cmd").write_text(body.format(py=PY), encoding="utf-8")
    return src


def _run(launcher: list[str], arg: str) -> tuple[int, dict[str, int], str]:
    work = Path(tempfile.mkdtemp(prefix="fwd_repro_w_"))
    proc = subprocess.run(
        [*launcher, arg], cwd=work, capture_output=True, text=True, creationflags=NO_WINDOW
    )
    stray = {n: (work / n).stat().st_size for n in sorted(os.listdir(work))}
    return proc.returncode, stray, (proc.stdout or proc.stderr).strip()[:70]


def _probe_cmd(src: Path) -> None:
    for body in _CMD_BODIES:
        for label, arg in _ARG_CASES.items():
            rc, stray, out = _run([str(src / f"{body}.cmd")], arg)
            print(f"  {body:17s} {label:17s} rc={rc:<4} stray={stray or '{}'} out={out!r}")


def _probe_ps1_twin(src: Path) -> None:
    """The `.ps1` twin is the candidate fix shape, so it is measured, not assumed clean.

    `render_ps1` splats via `@args` and never re-parses. Loaded by file path because
    `gen-launcher-shim.py` carries a hyphen and is not importable as a module name.
    """
    shim_path = REPO / "coordinator" / "bin" / "gen-launcher-shim.py"
    spec = importlib.util.spec_from_file_location("_gen_launcher_shim", shim_path)
    if spec is None or spec.loader is None:
        print(f"  SKIPPED — could not load {shim_path}")
        return
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    (src / "probe-cli.ps1").write_text(
        shim.render_ps1("probe-cli.py", python_bin_token=PY), encoding="utf-8", newline="\r\n"
    )
    launcher = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(src / "probe-cli.ps1")]
    for label, arg in {**_ARG_CASES, "caret": "abc^..def"}.items():
        rc, stray, out = _run(launcher, arg)
        print(f"  ps1               {label:17s} rc={rc:<4} stray={stray or '{}'} out={out!r}")


def main() -> int:
    if os.name != "nt":
        print("Windows-only: the defect is cmd.exe's re-parse of a substituted %*.")
        return 0
    src = _render_probe_tree()
    print("cmd forwarders — a non-empty `stray` is the defect:")
    _probe_cmd(src)
    print("\n.ps1 twin — expected clean on every row:")
    _probe_ps1_twin(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
