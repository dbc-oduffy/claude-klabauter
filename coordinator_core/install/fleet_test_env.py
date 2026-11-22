"""coordinator_core.install.fleet_test_env -- does each registered fleet repo's test suite collect?

`fleet_test_env_items` walks the repo registry and returns one `CheckItem` per worktree: a bounded
`pytest --collect-only` that FAILs naming the repo key and each missing module. With `install=True`
each repo's declared dev/test deps are pip-installed into its own `.venv` first.

Traps:
- Never pip-install into `engine_py`: another repo's dev pins would move the engine's own
  distributions. A repo without a `.venv` is collected under `engine_py` and a missing module FAILs.
- Never pass a pip override (`--break-system-packages`); a guarded interpreter's refusal is the FAIL.
- A repo reached after the overall deadline FAILs "never ran"; a skipped repo is never a pass.
"""

from __future__ import annotations

import configparser
import re
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Optional

from coordinator_core.install.setup_check import CheckItem
from coordinator_core.win_portability import no_console_creationflags

FLEET_COLLECT_TIMEOUT_S = 45
FLEET_COLLECT_TOTAL_BUDGET_S = 240
_DETAIL_CAP = 99
_INSTALL_TIMEOUT_S = 120

_NO_MODULE = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
_COLLECTED = re.compile(r"(?:(\d+)/)?(\d+) tests? collected")
_ERROR_LINE = re.compile(r"^(?:E\s+)?(\w*(?:Error|Exception)\b.*)$", re.MULTILINE)


def _run(argv: list, cwd: Path, timeout: float) -> "subprocess.CompletedProcess":
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _cap(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _DETAIL_CAP else text[: _DETAIL_CAP - 3] + "..."


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def _ini_has_section(path: Path, section: str) -> bool:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError):
        return False
    return parser.has_section(section)


def _is_python_test_repo(root: Path) -> bool:
    """pytest's own config set, plus a root `conftest.py` for a repo relying on defaults."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        tool = _read_toml(pyproject).get("tool", {})
        if isinstance(tool, dict) and "ini_options" in (tool.get("pytest") or {}):
            return True
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return True
    if (root / "tox.ini").is_file() and _ini_has_section(root / "tox.ini", "pytest"):
        return True
    return (root / "setup.cfg").is_file() and _ini_has_section(root / "setup.cfg", "tool:pytest")


def _requirement_lines(path: Path) -> list:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        spec = line.split(" #", 1)[0].strip()
        if spec and not spec.startswith(("#", "-")):
            out.append(spec)
    return out


def _declared_test_deps(root: Path) -> list:
    """Requirement specs a repo declares for testing: pyproject `test`/`dev` extras and dependency
    groups, then `requirements-dev.txt` / `requirements-test.txt`; de-duplicated, order kept."""
    specs: list = []
    data = _read_toml(root / "pyproject.toml")
    for table in (
        (data.get("project") or {}).get("optional-dependencies") or {},
        data.get("dependency-groups") or {},
    ):
        for group in ("test", "dev"):
            entries = table.get(group) if isinstance(table, dict) else None
            if isinstance(entries, list):
                specs.extend(e.strip() for e in entries if isinstance(e, str) and e.strip())
    for name in ("requirements-dev.txt", "requirements-test.txt"):
        specs.extend(_requirement_lines(root / name))
    return list(dict.fromkeys(specs))


def _venv_python(root: Path) -> Optional[Path]:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def _tail(proc: "subprocess.CompletedProcess") -> str:
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit {proc.returncode}"


def _install_deps(key: str, py: Path, root: Path, deps: list, timeout: float) -> Optional[CheckItem]:
    """pip-installs `deps` into the interpreter `py`; a FAIL item on failure, None on success."""
    name = f"fleet-test-env:{key}"
    try:
        proc = _run([str(py), "-m", "pip", "install", *deps], root, timeout)
    except subprocess.TimeoutExpired:
        return CheckItem(name, False, "never ran: dep install timed out")
    except OSError as exc:
        return CheckItem(name, False, _cap(f"never ran: dep install: {exc}"))
    if proc.returncode != 0:
        return CheckItem(name, False, _cap(f"dep install failed: {_tail(proc)}"))
    return None


def _collect(key: str, py: str, root: Path, timeout: float) -> CheckItem:
    name = f"fleet-test-env:{key}"
    argv = [py, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    try:
        proc = _run(argv, root, timeout)
    except subprocess.TimeoutExpired:
        return CheckItem(name, False, f"never ran: collect timed out after {int(timeout)}s")
    except OSError as exc:
        return CheckItem(name, False, _cap(f"never ran: interpreter unavailable: {exc}"))
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode == 0:
        match = None
        for match in _COLLECTED.finditer(output):
            pass
        count = int(match.group(2)) if match else sum(1 for ln in output.splitlines() if "::" in ln)
        if count > 0:
            return CheckItem(name, True, f"collected {count}")
        return CheckItem(name, False, "collected 0 tests")
    modules = list(dict.fromkeys(_NO_MODULE.findall(output)))
    if modules:
        return CheckItem(name, False, _cap("missing: " + ", ".join(modules)))
    error = _ERROR_LINE.search(output)
    return CheckItem(name, False, _cap(error.group(1) if error else f"collect failed: {_tail(proc)}"))


def fleet_test_env_items(bin_dir: Path, engine_py: str, *, install: bool) -> list:
    """One CheckItem per registered fleet worktree: does its test suite collect?

    Repos run sequentially under one `FLEET_COLLECT_TOTAL_BUDGET_S` deadline; a repo reached after it
    FAILs "never ran". `install=True` pip-installs declared deps into the repo's own `.venv` only.
    """
    from coordinator_core.install import git_perf_config

    walk = git_perf_config.iter_fleet_worktrees(bin_dir)
    if not walk.ok:
        why = walk.reason or "unknown"
        return [CheckItem("fleet-test-env", False, _cap(f"registry walk failed: {why} {walk.detail or ''}"))]

    deadline = time.monotonic() + FLEET_COLLECT_TOTAL_BUDGET_S
    items: list = []
    for entry in walk.items:
        kind, key, root = entry[0], entry[1], Path(entry[2])
        name = f"fleet-test-env:{key}"
        if kind == "missing":
            items.append(CheckItem(name, False, _cap(f"registry entry unreachable: {root}")))
            continue
        if kind == "error":
            items.append(CheckItem(name, False, _cap(f"classify failed: {entry[3]}")))
            continue
        if not _is_python_test_repo(root):
            items.append(CheckItem(name, True, "no python test config"))
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            items.append(CheckItem(name, False, "never ran: fleet budget exhausted"))
            continue
        venv_py = _venv_python(root)
        if install and venv_py is not None:
            deps = _declared_test_deps(root)
            if deps:
                failed = _install_deps(key, venv_py, root, deps, min(_INSTALL_TIMEOUT_S, remaining))
                if failed is not None:
                    items.append(failed)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    items.append(CheckItem(name, False, "never ran: fleet budget exhausted"))
                    continue
        py = str(venv_py) if venv_py is not None else engine_py
        items.append(_collect(key, py, root, min(FLEET_COLLECT_TIMEOUT_S, remaining)))
    return items
