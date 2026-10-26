"""Local loader for the vendored replay fixtures under `fixtures/`."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(name):
        directory = FIXTURES / name
        if not directory.is_dir():
            raise FileNotFoundError(f"replay fixture {name!r} not found under {FIXTURES}")
        return {"dir": directory}

    return _load
