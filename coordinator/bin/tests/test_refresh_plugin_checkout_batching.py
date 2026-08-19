"""test_refresh_plugin_checkout_batching — pytest coverage for the batched
interactive-partial-checkout leg in refresh-plugin-live-install.py.

Spec backlink: state/ledgers/amp-wave4-worklist.md W2 -- chunk C2's
amplification burn-down. `_handle_default`'s interactive-partial branch
previously spawned one `git checkout <ref> -- <f>` per approved file; a
single-item test passes identically before and after a batching change (the
exact gap this worklist's own C2 brief calls out as having shipped a wrong
batched fix elsewhere on 2026-08-19), so this file asserts the MULTI-item
shape explicitly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_plugin_live_install_checkout_batching",
        _BIN_DIR / "refresh-plugin-live-install.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def test_checkout_approved_files_makes_one_call_for_many_files(monkeypatch, tmp_path):
    calls = []

    def fake_git(args, cwd):
        calls.append((args, cwd))

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(_mod, "_git", fake_git)

    approved = ["a.md", "sub/b.py", "c.txt"]
    result = _mod._checkout_approved_files("origin/main", approved, tmp_path)

    assert result.returncode == 0
    assert len(calls) == 1, f"expected exactly ONE git checkout call for the whole approved set, got {calls}"
    args, cwd = calls[0]
    assert args[:3] == ["checkout", "origin/main", "--"]
    assert args[3:] == approved
    assert cwd == tmp_path


def test_checkout_approved_files_single_file_still_one_call(monkeypatch, tmp_path):
    """Single-item shape must not regress -- still exactly one call."""
    calls = []

    def fake_git(args, cwd):
        calls.append(args)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(_mod, "_git", fake_git)

    _mod._checkout_approved_files("origin/main", ["only.md"], tmp_path)

    assert len(calls) == 1
    assert calls[0] == ["checkout", "origin/main", "--", "only.md"]


def test_checkout_approved_files_propagates_failure(monkeypatch, tmp_path):
    def fake_git(args, cwd):
        class _Result:
            returncode = 1

        return _Result()

    monkeypatch.setattr(_mod, "_git", fake_git)

    result = _mod._checkout_approved_files("origin/main", ["a.md", "b.md"], tmp_path)

    assert result.returncode == 1
