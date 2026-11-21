"""The outside-importer scan of halt eviction reads every dirty Python path outside the parked set, so its
cost scaled with that count (406ms process CPU at 805 paths). A gate now keeps files that cannot name a
parked module out of the import regex and the index. The keep decision must equal the ungated scan's."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_token_index
from coordinator_core.ops.dispatch_emit import halted_record_eviction as hre

PARKED = ["pkg/core.py", "pkg/sub/__init__.py", "a/util.py", "lone.py"]
LINKED = {
    "imp_abs.py": "import lone\n",
    "imp_from.py": "from pkg import core\n",
    "pkg/imp_rel.py": "from . import core\n",
    # No parked stem in the text: the target comes from the importer's own directory chain.
    "a/util/x.py": "from . import y\n",
    "imp_sub.py": "import pkg.sub.deep\n",
}
UNLINKED = {"free.py": "import os\n", "other/mod.py": "from . import sibling\n"}


class _Head:
    def head_text(self, _path: str) -> str:
        return ""


@pytest.fixture(autouse=True)
def _no_persisted_index(monkeypatch):
    monkeypatch.setattr(review_token_index, "_cache_path", lambda _key: None)
    review_token_index._MEM.clear()
    yield
    review_token_index._MEM.clear()


def _tree(root: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return sorted(files)


def _kept(root: Path, parked: list[str], outside: list[str]) -> set:
    review_token_index._MEM.clear()
    return hre._kept_by_outside_imports(root, parked, outside, _Head())


def test_gate_keeps_the_ungated_decision_on_a_large_synthetic_input(tmp_path, monkeypatch) -> None:
    files = {p: "X = 1\n" for p in PARKED} | LINKED | UNLINKED
    files |= {f"filler/d{i % 20}/m{i}.py": f"import os\nVALUE_{i} = {i}\n" for i in range(600)}
    outside = _tree(tmp_path, files)
    outside = [p for p in outside if p not in PARKED]
    gated = _kept(tmp_path, PARKED, outside)
    monkeypatch.setattr(hre, "_outside_link_gate", lambda *_a: (lambda _rel, _data: True))
    ungated = _kept(tmp_path, PARKED, outside)
    assert gated == ungated
    assert {"lone.py", "pkg/core.py", "pkg/sub/__init__.py", "a/util.py"} <= gated


def test_gate_off_for_a_root_package_admits_every_file() -> None:
    admits = hre._outside_link_gate(["__init__.py", "m.py"], {"__init__.py": ""})
    assert admits("anything/else.py", b"X = 1\n")


def test_gate_rejects_only_files_with_no_parked_stem() -> None:
    admits = hre._outside_link_gate(["pkg/core.py"], {})
    assert not admits("free.py", b"import os\n")
    assert admits("free.py", b"from pkg import core\n")
    assert admits("pkg/core/x.py", b"X = 1\n")


def test_outside_scan_stays_under_the_brightline_at_805_paths(tmp_path) -> None:
    files = {p: "X = 1\n" for p in PARKED}
    files |= {f"filler/d{i % 20}/m{i}.py": "import os\nfrom collections import abc\n" * 20 for i in range(805)}
    outside = [p for p in _tree(tmp_path, files) if p not in PARKED]
    _kept(tmp_path, PARKED, outside)  # first call imports the lazy modules; the engine is warm
    runs = []
    for _ in range(3):
        start = time.process_time()
        assert _kept(tmp_path, PARKED, outside) == set()
        runs.append((time.process_time() - start) * 1000)
    assert min(runs) < 200, runs
