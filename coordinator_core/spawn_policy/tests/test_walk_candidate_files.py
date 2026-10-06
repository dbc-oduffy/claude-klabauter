"""walk_candidate_files / discover_source_files against a frozen rglob reference."""

from __future__ import annotations

import pathlib

from coordinator_core.spawn_policy.detect import (
    DEFAULT_EXCLUDE,
    ExcludedReport,
    _is_python_source,
    discover_source_files,
    walk_candidate_files,
)


def _reference_discover(root: pathlib.Path, exclude=DEFAULT_EXCLUDE):
    discovered = []
    excluded_dirs: set[str] = set()
    suppressed = 0
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(root)
        parts = rel.parts
        excluded_at = None
        for idx, part in enumerate(parts):
            if part in exclude:
                excluded_at = "/".join(parts[: idx + 1])
                break
        if excluded_at is not None:
            excluded_dirs.add(excluded_at)
            if file_path.suffix == ".py":
                suppressed += 1
            continue
        if not _is_python_source(file_path, rel):
            continue
        discovered.append((rel.as_posix(), file_path))
    return discovered, ExcludedReport(paths=sorted(excluded_dirs), suppressed_site_count=suppressed)


def _build(root: pathlib.Path) -> None:
    files = {
        "a.py": "x = 1\n",
        "B.py": "y = 2\n",
        "Zed/inner.py": "z = 3\n",
        "zed/other.py": "z = 4\n",
        "scratch/snap.py": "import os\n",
        "scratch/notes.txt": "hi\n",
        "node_modules/pkg/index.py": "q = 1\n",
        "node_modules/pkg/data.json": "{}\n",
        "build/only.txt": "no py here\n",
        "pkg/__pycache__/m.cpython-311.pyc": "junk",
        "tools/run": "#!/usr/bin/env python3\nprint(1)\n",
        "tools/shell": "#!/bin/bash\necho hi\n",
        "bin/cli": '"""doc"""\nprint(1)\n',
        "bin/.percolate-ignore": "# not python !!\n",
        "bin/plain": "just text\n",
        "deep/er/mod.PY": "x = 1\n",
        "deep/er/venv": "a file named like an excluded dir\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "empty" / "venv").mkdir(parents=True)


def test_discover_matches_rglob_reference(tmp_path):
    _build(tmp_path)
    assert discover_source_files(tmp_path) == _reference_discover(tmp_path)


def test_discover_matches_reference_custom_exclude(tmp_path):
    _build(tmp_path)
    ex = ("Zed", "bin")
    assert discover_source_files(tmp_path, exclude=ex) == _reference_discover(tmp_path, ex)


def test_pruning_walk_omits_excluded_and_reports_none(tmp_path):
    _build(tmp_path)
    candidates, report = walk_candidate_files(tmp_path)
    assert report is None
    rels = [c.rel_posix for c in candidates]
    assert not any(r.split("/")[0] in DEFAULT_EXCLUDE for r in rels)
    counted, _ = walk_candidate_files(tmp_path, count_excluded=True)
    assert rels == [c.rel_posix for c in counted]


def test_candidates_carry_stat(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (candidate,), _ = walk_candidate_files(tmp_path)
    st = (tmp_path / "a.py").stat()
    assert candidate.size == st.st_size
    assert candidate.mtime_ns == st.st_mtime_ns
    assert candidate.path == tmp_path / "a.py"
    assert candidate.rel_posix == "a.py"
