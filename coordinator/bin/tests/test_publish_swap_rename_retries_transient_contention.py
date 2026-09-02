"""A swap rename loses to a millisecond-long holder unless it retries.

WHY THIS FILE EXISTS. A publish round refused with `[WinError 5] Access is denied`
renaming `<mirror>/coordinator_core` aside, and the whole engine row fail-closed. The
obvious explanation -- the resident warm engine serving out of that clone pins its own
package -- was filed, then measured and RETRACTED: both servers' cwd is the mirror root
(which blocks renaming the root, not its children), neither holds an open file in the
clone, and probing the exact access a rename needs succeeded 589/589 over 30s with both
servers up.

Sampling that probe at 20ms through live hook traffic caught the real cause: the directory
went unrenamable with `ERROR_SHARING_VIOLATION` for 21 MILLISECONDS, then freed. The swap
was one un-retried `os.rename` fired at an arbitrary instant against a package ~50
concurrent sessions read continuously. It did not fail because the destination was
unavailable; it failed because it rolled the dice once.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "publish_under_test", Path(__file__).resolve().parents[1] / "publish.py"
)
publish = importlib.util.module_from_spec(_SPEC)
# Registered before exec: `publish.py` defines dataclasses, and `dataclasses` resolves a
# class's own module out of `sys.modules` while processing it. Loading it unregistered
# raises `AttributeError: 'NoneType' object has no attribute '__dict__'` at import time --
# the same shape `test_publish_refusal_record.py`'s own loader avoids this way.
sys.modules[_SPEC.name] = publish
_SPEC.loader.exec_module(publish)


def _oserror(winerror: int) -> OSError:
    exc = OSError(13, "Access is denied")
    exc.winerror = winerror
    return exc


def test_a_rename_that_frees_after_a_few_attempts_succeeds(tmp_path, monkeypatch):
    """The measured shape: held for a moment, then fine.

    Mutation check: point `_rename_with_retry` back at a bare `os.rename` and this fails
    with the first `ERROR_SHARING_VIOLATION` -- which is the production bug exactly.
    """
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    calls = {"n": 0}
    real = os.rename

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 4:
            raise _oserror(32)
        return real(a, b)

    monkeypatch.setattr(publish.os, "rename", flaky)
    publish._rename_with_retry(src, dst)
    assert calls["n"] == 4
    assert dst.is_dir() and not src.exists()


@pytest.mark.parametrize("winerror", [5, 32])
def test_a_destination_held_for_the_whole_budget_still_refuses(tmp_path, monkeypatch, winerror):
    """Retrying contention must not turn a wedged destination into a silent pass.

    The LAST exception is re-raised unchanged, so the caller's refusal record and its
    `failing_operation` label are byte-identical to what they were before the retry existed.
    """
    monkeypatch.setattr(publish, "_SWAP_RENAME_DEADLINE_SECS", 0.1)

    def always_held(a, b):
        raise _oserror(winerror)

    monkeypatch.setattr(publish.os, "rename", always_held)
    with pytest.raises(OSError) as caught:
        publish._rename_with_retry(tmp_path / "a", tmp_path / "b")
    assert caught.value.winerror == winerror


def test_a_non_contention_error_is_not_retried(tmp_path, monkeypatch):
    """Waiting cannot make a missing source appear.

    Retrying every `OSError` would hide a real defect behind a ten-second pause, so the
    allowlist is the point: one attempt, and the error surfaces immediately.
    """
    calls = {"n": 0}

    def missing(a, b):
        calls["n"] += 1
        exc = OSError(2, "No such file or directory")
        exc.winerror = 2
        raise exc

    monkeypatch.setattr(publish.os, "rename", missing)
    with pytest.raises(OSError):
        publish._rename_with_retry(tmp_path / "a", tmp_path / "b")
    assert calls["n"] == 1


def test_both_swap_branches_route_their_renames_through_the_retry():
    """Half a fix is what leaves the next round refusing on the other branch.

    The failing round recorded `swap_branch: whole-tree`, but the root-dest branch runs the
    same three renames against the same tree under the same live traffic. A bare `os.rename`
    surviving in either swap path is this bug still shipped.
    """
    import ast
    import inspect
    import textwrap

    # AST, not a substring scan: both docstrings QUOTE `os.rename(...)` while explaining
    # the swap, so a text match reports the prose and passes only if the explanation is
    # deleted. Call sites are the thing under test.
    for fn in (
        publish._swap_publish_staging_into_dest,
        publish._swap_publish_staging_entry,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        direct = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rename"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ]
        assert not direct, (
            "%s calls os.rename directly at line(s) %s; every swap rename must go "
            "through _rename_with_retry"
            % (fn.__name__, [n.lineno for n in direct])
        )
