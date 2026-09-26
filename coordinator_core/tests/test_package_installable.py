"""
Packaging round-trip tests — prove `coordinator_core` installs as an
importable package, subpackages included, under both the editable and
real (wheel-style) install paths.

Nothing else in this suite exercises the install path: every other test runs
against the repo's own checkout, importable only because pytest's rootdir
(coordinator_core/) is on sys.path via cwd/PYTHONPATH. That masked the
2026-07-21 packaging defect — `pyproject.toml` had no `[build-system]` /
package-discovery config, so setuptools' flat-layout auto-discovery saw the
repo's working-data dirs (state/, archive/, scratch/, scratchpad/) as sibling
top-level packages and refused to install at all. Fixing that also surfaced a
second, more insidious failure mode: `coordinator_core/contract/` had no
`__init__.py`, so a real (non-editable) wheel silently DROPPED
`coordinator_core.contract` and everything nested beneath it (including
`cockpit_schema`, DoE's sole cockpit-contract regeneration path).

**Only the non-editable leg below actually guards that second regression.**
setuptools' PEP 660 editable install redirects each top-level package to its
source directory via a MAPPING dict keyed at top-level-package granularity
only (e.g. `{'coordinator_core': '<repo>/coordinator_core'}`); everything
beneath that entry then resolves by ordinary filesystem traversal against the
live source tree, so a subpackage-level discovery gap (a missing
`__init__.py` several levels down) is invisible to an editable install
regardless of what discovery actually found. A real install has no such
redirect — it only exposes what discovery + packaging actually captured, so
it is the only leg that can catch `coordinator_core.contract` (or anything
beneath it) silently going missing. The editable leg is still worth keeping:
it guards discovery-refusal (the flat-layout error above, which fails an
editable install too) and matches the day-to-day dev workflow. Both tests
build their target install into a throwaway venv and import from cwd OUTSIDE
the repo, so a cwd-based import can't produce a false green either way.

Spec backlink: cross-repo memo "coordinator_core is not pip-installable —
flat-layout discovery error kills regen-cockpit-schema.py on every machine".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _pristine_source_export(repo_root: Path, dest: Path) -> None:
    ls_files = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=_NO_CONSOLE,
    )
    assert ls_files.returncode == 0, f"git ls-files failed:\n{ls_files.stderr}"

    for rel_path in ls_files.stdout.splitlines():
        if not rel_path:
            continue
        src = repo_root / rel_path
        if not src.is_file():
            continue
        dst = dest / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _install_runtime_deps(python: Path) -> None:
    dep_install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "pydantic>=2",
            "psutil>=5.9",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=_NO_CONSOLE,
    )
    assert dep_install.returncode == 0, (
        f"failed to install coordinator_core's runtime deps into the test "
        f"venv:\n{dep_install.stderr}"
    )


def _assert_deep_import(python: Path, cwd: Path) -> None:
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import coordinator_core; "
            "import coordinator_core.contract.cockpit_schema.emit_schema; "
            "print('IMPORT_OK')",
        ],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=60,
        creationflags=_NO_CONSOLE,
    )
    assert probe.returncode == 0 and "IMPORT_OK" in probe.stdout, (
        f"installed coordinator_core did not expose the deep subpackage "
        f"import from outside the repo:\nstdout={probe.stdout}\n"
        f"stderr={probe.stderr}"
    )


@pytest.mark.slow
def test_editable_install_exposes_deep_subpackage_outside_repo_cwd(
    tmp_path: Path,
) -> None:
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    python = _venv_python(venv_dir)

    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "-e",
            str(_PROJECT_ROOT),
            "--no-deps",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=_NO_CONSOLE,
    )
    assert install.returncode == 0, (
        f"pip install -e . --no-deps failed (this is the packaging-discovery "
        f"signal itself, not a dependency problem):\n{install.stderr}"
    )

    _install_runtime_deps(python)
    _assert_deep_import(python, cwd=tmp_path)


@pytest.mark.slow
def test_non_editable_install_exposes_deep_subpackage_outside_repo_cwd(
    tmp_path: Path,
) -> None:
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    python = _venv_python(venv_dir)

    # _PROJECT_ROOT directly: this is the leg that actually catches
    src_export = tmp_path / "src"
    _pristine_source_export(_PROJECT_ROOT, src_export)

    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            str(src_export),
            "--no-deps",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=_NO_CONSOLE,
    )
    assert install.returncode == 0, (
        f"pip install . --no-deps failed (this is the packaging-discovery "
        f"signal itself, not a dependency problem):\n{install.stderr}"
    )

    _install_runtime_deps(python)
    _assert_deep_import(python, cwd=tmp_path)
