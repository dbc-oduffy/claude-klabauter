
from __future__ import annotations

import warnings
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEPT_DIRS = ("coordinator_core", "coordinator/bin")


def _iter_py_files():
    for rel_dir in _SWEPT_DIRS:
        root = _REPO_ROOT / rel_dir
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.is_file():
                yield path


def test_every_swept_module_compiles():
    failures: list[str] = []
    for path in _iter_py_files():
        try:
            source = path.read_bytes()
        except OSError:
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(_REPO_ROOT)}: {exc}")

    assert not failures, "SyntaxError in swept module(s):\n" + "\n".join(failures)


def test_every_swept_module_is_free_of_syntax_warnings():
    failures: list[str] = []
    for path in _iter_py_files():
        try:
            source = path.read_bytes()
        except OSError:
            continue
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(source, str(path), "exec")
            except SyntaxError:
                continue
            for w in caught:
                if issubclass(w.category, SyntaxWarning):
                    failures.append(
                        f"{path.relative_to(_REPO_ROOT)}:{w.lineno}: {w.message}"
                    )

    assert not failures, "SyntaxWarning in swept module(s):\n" + "\n".join(failures)
