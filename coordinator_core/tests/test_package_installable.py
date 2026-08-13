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
`cockpit_schema`, coordinator-claude's sole cockpit-contract regeneration path).

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

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Every subprocess call below launches a venv's python.exe on Windows; suppress
# the console-popup / focus-steal that AllocConsole() would otherwise trigger
# under the headless Bash-tool parent. No-op (0) on macOS/Linux.
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-safe-env-suppressed


def _venv_python(venv_dir: Path) -> Path:
    # Cross-platform venv layout — Windows is first-class here (see CLAUDE.md
    # § Runtime conventions), not an afterthought bolted on later.
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _pristine_source_export(repo_root: Path, dest: Path) -> None:
    """
    Copy the repo's tracked files into `dest`, driven by `git ls-files`
    rather than a raw directory copy.

    This is deliberately working-tree-faithful, not `git archive HEAD`: an
    archive of HEAD tests the last commit, not the checkout the developer is
    actually holding, so it would silently fail to catch an *uncommitted*
    packaging break — and, more immediately, it would fail to reproduce the
    exact false-green this test exists to close (moving
    `coordinator_core/contract/__init__.py` aside on disk without committing
    that move; HEAD still has the file, so an archive of HEAD still has it
    too). `git ls-files` gives the tracked-path list without ever touching
    git's committed blobs — each path is read from the live working tree, so
    a tracked-but-currently-absent file (moved aside, not `git rm`'d) is
    copied over as absent, faithfully reproducing the regression instead of
    masking it.

    This also satisfies the build-ephemera exclusion this test depends on:
    `build/`, `*.egg-info/`, `__pycache__/`, and `.venv/` are gitignored and
    never appear in `git ls-files` output, so a stale build/ or egg-info/
    sitting in the real repo root can never leak into the export.
    """
    ls_files = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        timeout=60,
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
    # The deep-import target (coordinator_core.contract.cockpit_schema) has a
    # real, non-optional transitive dependency on pydantic (see
    # coordinator_core/contract/cockpit_schema/emission_scope.py), so a plain
    # `--no-deps` venv cannot import it — that failure would be a dependency
    # gap, not a packaging regression, and asserting on it would be a false
    # red. Installing just the runtime deps here keeps the packaging
    # assertion in each caller isolated while still letting its import
    # assertion be genuine rather than a metadata-only proxy.
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
    # cwd is outside the repo root, so a cwd-based import cannot mask a
    # packaging failure the way it does for the rest of this suite (see
    # module docstring).
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

    # --no-deps isolates the packaging-discovery signal from dependency
    # resolution: a broken discovery config fails here regardless of whether
    # PyPI is reachable or pydantic/psutil happen to be installable.
    # --no-cache-dir: an editable install re-resolves against source on
    # every import, so a cached wheel can't mask this leg's own regression —
    # but this repo's pip cache is still shared machine-wide, and leaving a
    # stale coordinator_core wheel in it is exactly the ambient state that
    # produced the false green this hardening closes. Pass it everywhere on
    # principle, even on the leg it doesn't structurally protect.
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

    # Install from a pristine tracked-files export in tmp_path, never from
    # _PROJECT_ROOT directly: this is the leg that actually catches
    # coordinator_core.contract silently going missing (see module
    # docstring), so it must not be able to false-green off a stale
    # repo-root build/ or *.egg-info/ that still has the old package layout
    # baked in. See _pristine_source_export for why this is a working-tree
    # copy and not `git archive HEAD`.
    src_export = tmp_path / "src"
    _pristine_source_export(_PROJECT_ROOT, src_export)

    # No -e: a real install only exposes what discovery + packaging actually
    # captured, unlike an editable install's whole-package source-tree
    # redirect (see module docstring). --no-deps isolates the
    # packaging-discovery signal from dependency resolution. --no-cache-dir
    # defeats pip's wheel cache — the exact mechanism that produced this
    # test's original false green (a previously built wheel, still
    # containing the pre-regression package layout, served instead of
    # rebuilding from src_export).
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
