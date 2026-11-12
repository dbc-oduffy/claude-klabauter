"""Guard: a coordinator/lib .py admitted by a klabauter lib row must have its source .cmd twin admitted too."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB_DIR = REPO / "coordinator" / "lib"
LIB_ROWS = ("claude-klabauter-lib", "claude-klabauter-coordinator-lib")

sys.path.insert(0, str(LIB_DIR))
from percolate.targets import parse_portable_rows  # noqa: E402


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
    # Raw portable rows, not load_targets: resolving a row needs the machine-local
    # publish.mirrors.* registry, which the allowlist check must not depend on.
    rows = [
        r for r in parse_portable_rows(REPO / "setup" / "publish-targets.portable")
        if r["name"] == row_name
    ]
    assert rows, f"row {row_name} not found"
    allowlist = {e.strip() for e in rows[0]["allowlist"].split(",") if e.strip()}
    missing = missing_twins(allowlist, LIB_DIR)
    assert not missing, f"row {row_name} admits .py but omits .cmd twin(s): {missing}"
