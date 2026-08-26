"""The two pytest configs must register the SAME marker set.

`coordinator_core/pytest.ini` and the root `pyproject.toml`'s
`[tool.pytest.ini_options]` are both live: pytest picks the config CLOSEST to the
test-path argument, so `pytest coordinator_core/...` reads the former and a bare
`pytest` from the project root reads the latter. A marker registered in only one
resolves under only one invocation, emitting `PytestUnknownMarkWarning` under the
other — and hard-failing under `--strict-markers`.

This has already happened twice, both times found by hand: `cadence`/`pending_fix`/
`designed_red` landed in pyproject.toml on 2026-07-22 and were missing from
pytest.ini until 2026-07-28 (recorded in that file's own header), and
`real_machine_mutation` landed in pyproject.toml on 2026-08-26 and was missing
here until the warning surfaced minutes later. Both configs' headers warn about
the trap; neither warning is an artifact. This test is.
"""
from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTEST_INI = _REPO_ROOT / "coordinator_core" / "pytest.ini"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _marker_names(raw_entries) -> set[str]:
    """A marker entry is ``"<name>: <description>"`` — compare NAMES only, so a
    reworded description in one config is not a failure."""
    names = set()
    for entry in raw_entries:
        entry = entry.strip()
        if entry:
            names.add(entry.split(":", 1)[0].strip())
    return names


def _ini_markers() -> set[str]:
    parser = configparser.ConfigParser()
    parser.read(_PYTEST_INI, encoding="utf-8")
    return _marker_names(parser["pytest"]["markers"].splitlines())


def _pyproject_markers() -> set[str]:
    with _PYPROJECT.open("rb") as fh:
        doc = tomllib.load(fh)
    return _marker_names(doc["tool"]["pytest"]["ini_options"]["markers"])


def test_both_pytest_configs_register_the_same_markers():
    ini = _ini_markers()
    proj = _pyproject_markers()

    assert ini and proj, "a marker list parsed empty — the config shape moved"

    missing_from_ini = sorted(proj - ini)
    missing_from_pyproject = sorted(ini - proj)

    assert not missing_from_ini, (
        "markers registered in pyproject.toml but not coordinator_core/pytest.ini "
        f"-- a `pytest coordinator_core/...` run warns on each: {missing_from_ini}"
    )
    assert not missing_from_pyproject, (
        "markers registered in coordinator_core/pytest.ini but not pyproject.toml "
        f"-- a bare `pytest` from the project root warns on each: {missing_from_pyproject}"
    )
