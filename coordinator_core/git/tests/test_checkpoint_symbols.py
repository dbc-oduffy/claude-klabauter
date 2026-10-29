"""Tests for checkpoint_symbols.removed_symbol_refusal against a real tmp git repo."""

import subprocess

from coordinator_core.git.checkpoint_symbols import removed_symbol_refusal


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp_path, files):
    _git(tmp_path, "init", "-q")
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    return {rel: text.encode() for rel, text in files.items()}


LIB = "def keep():\n    pass\n\ndef gone():\n    pass\n"
LIB_NEW = "def keep():\n    pass\n"


def test_outside_importer_refuses(tmp_path):
    head = _repo(tmp_path, {"pkg/lib.py": LIB, "pkg/use.py": "from pkg.lib import gone\ngone()\n"})
    msg = removed_symbol_refusal(tmp_path, {"pkg/lib.py": LIB_NEW.encode()}, {"pkg/lib.py": head["pkg/lib.py"]})
    assert msg and "removes gone from pkg/lib.py" in msg and "pkg/use.py" in msg


def test_untracked_caller_refuses(tmp_path):
    head = _repo(tmp_path, {"pkg/lib.py": LIB})
    (tmp_path / "pkg" / "fresh.py").write_text("from pkg.lib import gone\ngone()\n", encoding="utf-8")
    msg = removed_symbol_refusal(tmp_path, {"pkg/lib.py": LIB_NEW.encode()}, {"pkg/lib.py": head["pkg/lib.py"]})
    assert msg and "pkg/fresh.py" in msg


def test_caller_inside_staged_set_passes(tmp_path):
    head = _repo(tmp_path, {"pkg/lib.py": LIB, "pkg/use.py": "from pkg.lib import gone\ngone()\n"})
    staged = {"pkg/lib.py": LIB_NEW.encode(), "pkg/use.py": b"x = 1\n"}
    assert removed_symbol_refusal(tmp_path, staged, {"pkg/lib.py": head["pkg/lib.py"]}) is None


def test_rename_with_attribute_caller_refuses(tmp_path):
    head = _repo(tmp_path, {"pkg/lib.py": LIB, "pkg/use.py": "import pkg.lib as L\nL.gone()\n"})
    new = LIB.replace("gone", "renamed").encode()
    msg = removed_symbol_refusal(tmp_path, {"pkg/lib.py": new}, {"pkg/lib.py": head["pkg/lib.py"]})
    assert msg and "gone" in msg and "pkg/use.py" in msg


def test_no_removal_passes(tmp_path):
    head = _repo(tmp_path, {"pkg/lib.py": LIB, "pkg/use.py": "from pkg.lib import gone\n"})
    added = (LIB + "def more():\n    pass\n").encode()
    assert removed_symbol_refusal(tmp_path, {"pkg/lib.py": added}, {"pkg/lib.py": head["pkg/lib.py"]}) is None


def test_deleted_file_refuses(tmp_path):
    head = _repo(tmp_path, {"pkg/lib.py": LIB, "pkg/use.py": "from pkg.lib import keep\n"})
    msg = removed_symbol_refusal(tmp_path, {"pkg/lib.py": None}, {"pkg/lib.py": head["pkg/lib.py"]})
    assert msg and "keep" in msg
