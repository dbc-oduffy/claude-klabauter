"""tests/test_repo_census.py — Tests for bin/repo-census.py.

Purpose: exercises the census, per-language import/dependency edge extraction
(Python + JS/TS; a spot-check of Go), cross-reference resolution, and --json
output of the repo-census CLI against a synthetic fixture tree built in a
tempdir — never against a real sibling repo (repo-census's whole point is to
work on repos it has never seen). Not to be confused with the tree-sitter
`repomap` tool's own test suite (`coordinator_core/tests/test_generate_repomap.py`)
— these two are distinct tools, see repo-census.py's module docstring.

Spec backlink: coordinator/pipelines/deep-research/repo-research-internals.md
    § Phase 1.5 — Repomap Generation (example-doctrine-repo)

Run: python3 -m pytest <settings-home>/coordinator/bin/tests/test_repo_census.py
"""

import json
import os
import subprocess
import sys
import tempfile

from coordinator_core.win_portability import no_console_creationflags

_HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.normpath(os.path.join(_HERE, "..", "repo-census.py"))


def _run(*args):
    proc = subprocess.run(
        [sys.executable, CLI, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **no_console_creationflags(),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write(root, relpath, content):
    path = os.path.join(root, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _build_fixture(root):
    """A small polyglot repo: Python (dominant) + JS/TS (second), a vendored
    dir that must be excluded by the default skip-list, and a .gitignore'd
    scratch dir that must be excluded via gitignore parsing.
    """
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/util.py", "def helper():\n    return 1\n")
    _write(
        root,
        "pkg/main.py",
        "import os\n"
        "import sys\n"
        "from pkg.util import helper\n"
        "from pkg.util import helper as h2\n",
    )
    _write(
        root,
        "pkg/other.py",
        "from pkg.util import helper\n"
        "import os\n",
    )
    _write(root, "src/index.ts", "import { widget } from './widget';\n")
    _write(root, "src/widget.ts", "export const widget = 1;\n")
    _write(
        root,
        "src/app.js",
        "const widget = require('./widget');\n"
        "import { widget as w2 } from './widget';\n",
    )
    # Vendored — must be excluded by the fixed skip-list regardless of .gitignore.
    _write(root, "node_modules/leftpad/index.js", "import { widget } from './widget';\n")
    # .gitignore'd scratch dir — must be excluded by gitignore parsing.
    _write(root, ".gitignore", "scratch/\n*.log\n")
    _write(root, "scratch/notes.py", "import shouldnotcount\n")
    _write(root, "build.log", "not source\n")
    return root


def test_census_excludes_vendor_and_gitignored():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc, out, err = _run(tmp, "--json")
        assert rc == 0, "exit {}: {}".format(rc, err)
        data = json.loads(out)
        exts = {row["extension"]: row["count"] for row in data["census"]}
        assert exts.get(".py") == 4, "expected 4 .py files (pkg/*), got {}".format(exts.get(".py"))
        assert ".log" not in exts, "build.log should be gitignored out of the census"
        # node_modules/leftpad/index.js must not inflate the .js count.
        assert exts.get(".js") == 1, "expected 1 .js file (node_modules excluded), got {}".format(exts.get(".js"))


def test_python_edge_extraction():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc, out, err = _run(tmp, "--json", "--languages", "python")
        assert rc == 0, "exit {}: {}".format(rc, err)
        data = json.loads(out)
        edges = {row["module"]: row["count"] for row in data["edges"]["python"]}
        # "os" imported in main.py and other.py -> 2 occurrences.
        assert edges.get("os") == 2, "expected os count 2, got {} ({})".format(edges.get("os"), edges)
        # "pkg.util" imported 3 times across main.py (x2) + other.py.
        assert edges.get("pkg.util") == 3, "expected pkg.util count 3, got {} ({})".format(edges.get("pkg.util"), edges)
        # scratch/notes.py is gitignored -- its "shouldnotcount" import must not appear.
        assert "shouldnotcount" not in edges, "gitignored file's import leaked into edges"


def test_js_ts_edge_extraction():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc, out, err = _run(tmp, "--json", "--languages", "js_ts")
        assert rc == 0, "exit {}: {}".format(rc, err)
        data = json.loads(out)
        edges = {row["module"]: row["count"] for row in data["edges"]["js_ts"]}
        # './widget' appears in index.ts (from), app.js (require), app.js (from) = 3.
        # node_modules/leftpad's occurrence must NOT count (file excluded).
        assert edges.get("./widget") == 3, "expected ./widget count 3, got {} ({})".format(edges.get("./widget"), edges)


def test_cross_reference_counts_distinct_files():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc, out, err = _run(tmp, "--json", "--languages", "python")
        assert rc == 0, "exit {}: {}".format(rc, err)
        data = json.loads(out)
        xref = {row["module"]: row for row in data["cross_references"]["python"]}
        # pkg.util is referenced from 2 DISTINCT files (main.py, other.py) even
        # though main.py imports it twice (line-count 3 in the edges table).
        entry = xref.get("pkg.util")
        assert entry is not None, "pkg.util missing from cross-references: {}".format(xref)
        assert entry["referencing_files"] == 2, "expected 2 distinct referencing files, got {}".format(entry["referencing_files"])
        assert entry["resolved_path"] == "pkg/util.py", "expected resolved_path pkg/util.py, got {}".format(entry["resolved_path"])


def test_go_edge_extraction_block_and_single_line():
    with tempfile.TemporaryDirectory() as tmp:
        _write(
            tmp,
            "main.go",
            'package main\n\n'
            'import "fmt"\n\n'
            'import (\n'
            '\t"os"\n'
            '\tlocal "example.com/foo/bar"\n'
            ')\n',
        )
        rc, out, err = _run(tmp, "--json", "--languages", "go")
        assert rc == 0, "exit {}: {}".format(rc, err)
        data = json.loads(out)
        modules = {row["module"] for row in data["edges"]["go"]}
        expected = {"fmt", "os", "example.com/foo/bar"}
        assert expected.issubset(modules), "expected {} subset of {}".format(expected, modules)


def test_dominant_language_auto_selects_top_two():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc, out, err = _run(tmp, "--json")
        assert rc == 0, "exit {}: {}".format(rc, err)
        data = json.loads(out)
        assert data["dominant_languages"] == ["python", "js_ts"], "expected [python, js_ts], got {}".format(data["dominant_languages"])


def test_human_output_is_default_and_nonempty():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc, out, err = _run(tmp)
        assert rc == 0, "exit {}: {}".format(rc, err)
        assert "Repo census —" in out and "census" in out.lower(), (
            "human output missing expected sections: {!r}".format(out[:200])
        )


def test_nonexistent_repo_path_exits_nonzero():
    rc, out, err = _run("/definitely/does/not/exist/anywhere")
    assert rc != 0, "expected non-zero exit for missing repo path"


def test_unknown_language_override_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(tmp)
        rc, out, err = _run(tmp, "--languages", "cobol")
        assert rc != 0, "expected non-zero exit for unknown language override"
