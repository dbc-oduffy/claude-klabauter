"""install-claude-author-wrapper.py forwards exactly one --wrapper-src in either form."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "install-claude-author-wrapper.py"


def _load():
    spec = importlib.util.spec_from_file_location("install_claude_author_wrapper_bin", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _forwarded(monkeypatch, argv):
    mod = _load()
    seen = {}

    def fake_run_op_main(op, forwarded):
        seen["argv"] = list(forwarded)
        return 0

    monkeypatch.setattr(mod, "_resolve_run_op_main", lambda: fake_run_op_main)
    assert mod.main(argv) == 0
    return mod, seen["argv"]


def _count(argv):
    return sum(1 for a in argv if a == "--wrapper-src" or a.startswith("--wrapper-src="))


def test_space_form_not_duplicated(monkeypatch):
    _, out = _forwarded(monkeypatch, ["--wrapper-src", "X"])
    assert out == ["--wrapper-src", "X"]


def test_equals_form_not_duplicated(monkeypatch):
    _, out = _forwarded(monkeypatch, ["--wrapper-src=X"])
    assert out == ["--wrapper-src=X"]


def test_no_flag_appends_default_once(monkeypatch):
    mod, out = _forwarded(monkeypatch, [])
    assert out == ["--wrapper-src", mod._default_wrapper_src()]
    assert _count(out) == 1
