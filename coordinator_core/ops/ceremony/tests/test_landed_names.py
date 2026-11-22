"""`uncommitted_name_refusals`: a landing `.py` naming something only the worktree has is refused."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.ceremony.landed_names import uncommitted_name_refusals


def _tree(tmp_path: Path, landed: dict, worktree: dict):
    for rel, text in worktree.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    enc = {k: v.encode("utf-8") for k, v in landed.items()}
    return enc.get


BASE = {"pkg/__init__.py": "", "pkg/idx.py": "def add_run(): ...\n"}
WT = {**BASE, "pkg/idx.py": "def add_run(): ...\ndef record_claim(): ...\nCONST = 1\n"}


def _run(tmp_path, landed_extra, worktree_extra=None, paths=("pkg/caller.py",), base=None):
    landed = {**(base or BASE), **landed_extra}
    wt = {**WT, **landed_extra, **(worktree_extra or {})}
    return uncommitted_name_refusals(tmp_path, list(paths), _tree(tmp_path, landed, wt))


def test_lazy_import_attribute_call_refused(tmp_path):
    src = "def f():\n    from pkg import idx\n    idx.record_claim()\n"
    out = _run(tmp_path, {"pkg/caller.py": src})
    assert out == ["pkg/caller.py:3: names pkg.idx.record_claim, which exists only uncommitted"]


def test_getattr_literal_refused(tmp_path):
    src = "import pkg.idx as idx\nfn = getattr(idx, 'record_claim')\n"
    out = _run(tmp_path, {"pkg/caller.py": src})
    assert out == ["pkg/caller.py:2: names pkg.idx.record_claim, which exists only uncommitted"]


def test_from_import_constant_in_non_coordinator_core_package_refused(tmp_path):
    out = _run(tmp_path, {"pkg/caller.py": "from pkg.idx import CONST\n"})
    assert out == ["pkg/caller.py:1: names pkg.idx.CONST, which exists only uncommitted"]


def test_chain_through_dotted_import_refused(tmp_path):
    out = _run(tmp_path, {"pkg/caller.py": "import pkg.idx\npkg.idx.record_claim()\n"})
    assert out == ["pkg/caller.py:2: names pkg.idx.record_claim, which exists only uncommitted"]


def test_relative_import_refused(tmp_path):
    out = _run(tmp_path, {"pkg/caller.py": "from . import idx\nidx.record_claim()\n"})
    assert out == ["pkg/caller.py:2: names pkg.idx.record_claim, which exists only uncommitted"]


def test_module_only_in_worktree_refused(tmp_path):
    out = _run(tmp_path, {"pkg/caller.py": "from pkg import newmod\n"}, {"pkg/newmod.py": "X = 1\n"})
    assert out == ["pkg/caller.py:1: names pkg.newmod, which exists only uncommitted"]


def test_landing_both_is_clean(tmp_path):
    landed = {"pkg/caller.py": "from pkg import idx\nidx.record_claim()\n", "pkg/idx.py": WT["pkg/idx.py"]}
    assert _run(tmp_path, landed, paths=("pkg/caller.py", "pkg/idx.py")) == []


def test_committed_names_are_clean(tmp_path):
    src = "from pkg import idx\nidx.add_run()\ngetattr(idx, 'add_run')\n"
    assert _run(tmp_path, {"pkg/caller.py": src}) == []


def test_name_absent_everywhere_passes(tmp_path):
    src = "from pkg import idx\nidx.nowhere()\ngetattr(idx, 'also_nowhere')\n"
    assert _run(tmp_path, {"pkg/caller.py": src}) == []


def test_three_arg_and_non_literal_getattr_pass(tmp_path):
    src = "from pkg import idx\nn = 'record_claim'\ngetattr(idx, n)\ngetattr(idx, 'record_claim', None)\n"
    assert _run(tmp_path, {"pkg/caller.py": src}) == []


def test_open_module_passes(tmp_path):
    base = {**BASE, "pkg/idx.py": "from os.path import *\n"}
    out = _run(tmp_path, {"pkg/caller.py": "from pkg import idx\nidx.record_claim()\n"}, base=base)
    assert out == []


def test_import_error_guarded_import_passes(tmp_path):
    src = "try:\n    from pkg.idx import CONST\nexcept ImportError:\n    CONST = 0\n"
    assert _run(tmp_path, {"pkg/caller.py": src}) == []


def test_third_party_and_stdlib_attribute_passes(tmp_path):
    src = "import os\nimport json\nos.path.nothing\njson.nothing\n"
    assert _run(tmp_path, {"pkg/caller.py": src}) == []


def test_conflicting_alias_bindings_are_skipped(tmp_path):
    src = "def f():\n    from pkg import idx\ndef g():\n    from pkg import other as idx\n    idx.record_claim()\n"
    assert _run(tmp_path, {"pkg/caller.py": src, "pkg/other.py": "Y = 1\n"}) == []


def test_non_python_and_unparseable_paths_are_ignored(tmp_path):
    out = _run(tmp_path, {"pkg/caller.py": "def (:\n", "notes.md": "x"}, paths=("pkg/caller.py", "notes.md"))
    assert out == []


def test_reader_failure_is_undecided(tmp_path):
    def boom(rel):
        raise RuntimeError("pack read stalled")

    assert uncommitted_name_refusals(tmp_path, ["pkg/caller.py"], boom) is None
