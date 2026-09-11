"""test_publish_report_net_change -- pins what `publish.py ::
_report_published_diff` counts as a change, and the run-level net fold the
round manifest is built from (`_net_changed_paths`).

Two measured defects at claude-klabauter, 2026-09-11, both chronic:

- The root flat-mirror row's content-transform sweep reaches files a later
  row owns, and the two rows' tables disagree (base `path_rewrite`), so 46
  files were rewritten and then restored within one run. Each row truthfully
  printed `UPDATE:`, the union named 46 changes, and the round then found all
  46 identical to HEAD: "N reported change(s) not carried" on every commit.
- Bytecode generated at dest or staging between the staging copy and the
  comparison (3 `.pyc` UPDATEs, 1 `.pyc` REMOVE per round) was reported as
  payload -- uncommittable (gitignored), and the REMOVE recreated by the next
  import before the commit leg looked.

Negative-spec: runs no publish round and no git; the manifest block itself is
not called (it lives inside `main`).

Run: python -m pytest coordinator/bin/tests/test_publish_report_net_change.py -q
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_report_net_change_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _tree(root: Path, files: "dict[str, str]") -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def test_bytecode_is_never_reported_on_either_side(tmp_path):
    dest = _tree(
        tmp_path / "dest",
        {
            "pkg/mod.py": "x = 1\n",
            "pkg/__pycache__/mod.cpython-313.pyc": "old",
            "pkg/__pycache__/gone.cpython-313.pyc": "old",
        },
    )
    staging = _tree(
        tmp_path / "staging",
        {"pkg/mod.py": "x = 1\n", "pkg/__pycache__/mod.cpython-313.pyc": "new"},
    )
    buf = io.StringIO()
    changed, removed = publish._report_published_diff(
        staging, dest, publish.RunTotals(), out=buf
    )
    assert changed == frozenset() and removed == frozenset(), buf.getvalue()


def test_a_file_rewritten_and_restored_in_one_run_is_not_a_net_change(tmp_path):
    """Row A (whole-mirror sweep) rewrites `pkg/mod.py`; row B (its owner)
    writes the original back. Both report it; the net set does not."""
    dest = _tree(tmp_path / "dest", {"pkg/mod.py": "owner\n", "pkg/real.py": "old\n"})
    digests: "dict[Path, object]" = {}

    row_a = _tree(tmp_path / "a", {"pkg/mod.py": "rewritten\n", "pkg/real.py": "old\n"})
    changed_a, _ = publish._report_published_diff(
        row_a, dest, publish.RunTotals(), out=io.StringIO(), pre_round_digests=digests
    )
    assert changed_a == {"pkg/mod.py"}
    (dest / "pkg" / "mod.py").write_bytes(b"rewritten\n")

    row_b = _tree(
        tmp_path / "b", {"pkg/mod.py": "owner\n", "pkg/real.py": "new\n", "pkg/add.py": "n\n"}
    )
    changed_b, _ = publish._report_published_diff(
        row_b, dest, publish.RunTotals(), out=io.StringIO(), pre_round_digests=digests
    )
    assert changed_b == {"pkg/mod.py", "pkg/real.py", "pkg/add.py"}
    for rel in changed_b:
        (dest / rel).write_bytes((row_b / rel).read_bytes())

    union = {dest / rel for rel in changed_a | changed_b}
    assert publish._net_changed_paths(union, digests) == {
        dest / "pkg" / "real.py",
        dest / "pkg" / "add.py",
    }


def test_a_path_with_no_recorded_digest_is_kept(tmp_path):
    path = _tree(tmp_path, {"x.py": "1\n"}) / "x.py"
    assert publish._net_changed_paths({path}, {}) == {path}
