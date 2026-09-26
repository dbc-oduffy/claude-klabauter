from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTEST_INI = _REPO_ROOT / "coordinator_core" / "pytest.ini"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _marker_names(raw_entries) -> set[str]:
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
