"""Guard: a coordinator/lib .py admitted by a klabauter lib row must have its source .cmd twin admitted too."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB_DIR = REPO / "coordinator" / "lib"
LIB_ROWS = ("claude-klabauter-lib", "claude-klabauter-coordinator-lib")

sys.path.insert(0, str(LIB_DIR))
from percolate.targets import load_targets  # noqa: E402


def _allowlist(row: str) -> set[str]:
    # load_targets resolves a row to 6 fields; the allowlist is the last (index 5).
    return {e.strip() for e in row.split("|")[5].split(",") if e.strip()}


def missing_twins(allowlist: set[str], lib_dir: Path) -> list[str]:
    """Return .cmd names that exist in `lib_dir` beside an admitted .py but are not admitted."""
    return sorted(
        f"{Path(name).stem}.cmd"
        for name in allowlist
        if name.endswith(".py")
        and (lib_dir / f"{Path(name).stem}.cmd").is_file()
        and f"{Path(name).stem}.cmd" not in allowlist
    )


def test_guard_flags_omitted_twin_and_passes_admitted(tmp_path):
    (tmp_path / "a.cmd").write_text("")
    assert missing_twins({"a.py"}, tmp_path) == ["a.cmd"]
    assert missing_twins({"a.py", "a.cmd"}, tmp_path) == []
    assert missing_twins({"b.py"}, tmp_path) == []


@pytest.mark.parametrize("row_name", LIB_ROWS)
def test_lib_row_admits_cmd_twin_of_every_admitted_py(row_name):
    rows = [
        r for r in load_targets(REPO / "setup", target_filter=row_name)
        if r.split("|", 1)[0] == row_name
    ]
    assert rows, f"row {row_name} not resolved"
    missing = missing_twins(_allowlist(rows[0]), LIB_DIR)
    assert not missing, f"row {row_name} admits .py but omits .cmd twin(s): {missing}"
