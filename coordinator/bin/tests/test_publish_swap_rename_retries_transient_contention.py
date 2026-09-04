"""A swap rename loses to a millisecond-long holder unless it retries.

A 21ms `ERROR_SHARING_VIOLATION` window, measured under ~50 concurrent sessions -- not a
held destination. See `state/lessons/2026-09-02-the-obvious-mechanism-was-the-wrong-one-measure-before-you-file.md`
for how that was determined and why the more obvious explanation was wrong.
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
assert _SPEC is not None and _SPEC.loader is not None
publish = importlib.util.module_from_spec(_SPEC)
# Registered before exec: `publish.py` defines dataclasses, and `dataclasses` resolves a
# class's own module out of `sys.modules` while processing it. Loading it unregistered
# raises `AttributeError: 'NoneType' object has no attribute '__dict__'` at import time --
# the same shape `test_publish_refusal_record.py`'s own loader avoids this way.
sys.modules[_SPEC.name] = publish
_SPEC.loader.exec_module(publish)


def _oserror(winerror: int) -> PermissionError:
    exc = PermissionError(13, "Access is denied")
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

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 4:
            raise _oserror(32)
        return real(src, dst)

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

    def always_held(_src, _dst):
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

    def missing(_src, _dst):
        calls["n"] += 1
        exc = OSError(2, "No such file or directory")
        exc.winerror = 2
        raise exc

    monkeypatch.setattr(publish.os, "rename", missing)
    with pytest.raises(OSError):
        publish._rename_with_retry(tmp_path / "a", tmp_path / "b")
    assert calls["n"] == 1


def test_every_swap_publish_function_routes_its_renames_through_the_retry():
    """Half a fix is what leaves the next round refusing on the other branch.

    The failing round recorded `swap_branch: whole-tree`, but the root-dest branch runs the
    same three renames against the same tree under the same live traffic. A bare `os.rename`
    surviving in ANY swap function is this bug still shipped. Walking every `_swap_publish_*`
    function by name, rather than naming the two known today, means a third branch is
    covered by construction instead of needing this test edited to see it.
    """
    import ast
    import inspect
    import textwrap

    swap_fns = [
        obj
        for name, obj in vars(publish).items()
        if name.startswith("_swap_publish_") and inspect.isfunction(obj)
    ]
    assert swap_fns, "no _swap_publish_* functions found -- module shape changed"

    # AST, not a substring scan: docstrings QUOTE `os.rename(...)` while explaining the
    # swap, so a text match reports the prose and passes only if the explanation is
    # deleted. Call sites are the thing under test.
    for fn in swap_fns:
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


def test_a_permission_error_is_required_not_merely_an_oserror_with_the_winerror():
    """The 2026-08-10 ruling prescribes `PermissionError` only, and it is right.

    A blanket `except OSError` would retry `FileExistsError`/`NotADirectoryError`/
    `IsADirectoryError` -- permanent conditions where waiting changes nothing and the only
    effect is to delay the report. This pins the narrower catch: an `OSError` that merely
    carries a transient-looking `winerror` is NOT retried, because the exception type is
    part of the discrimination, not decoration on it.

    NOTE ON WHAT THIS DOES AND DOES NOT CHANGE. For the real failure -- winerror 5 or 32
    off a Windows rename -- narrowing `except OSError` to `except PermissionError` changes
    NOTHING at runtime: CPython raises those as `PermissionError` already. The narrowing
    buys the permanent-error exclusions above and matches the shape the 2026-08-10 ruling
    prescribed; it is not a behaviour fix, and should not be reported as one.
    """
    calls = {"n": 0}

    def not_a_permission_error(_src, _dst):
        calls["n"] += 1
        # errno 9 (EBADF), NOT 13: `OSError.__new__` maps errno 13 to PermissionError
        # automatically, so `OSError(13, ...)` IS a PermissionError and cannot express
        # "an OSError that merely looks transient". Getting this wrong makes the test
        # assert the opposite of what it reads as.
        exc = OSError(9, "Bad file descriptor")
        exc.winerror = 32
        raise exc

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(publish.os, "rename", not_a_permission_error)
        with _pytest.raises(OSError):
            publish._rename_with_retry(Path("a"), Path("b"))
    assert calls["n"] == 1, "a bare OSError must not enter the retry loop"


def test_a_failing_restore_chains_from_the_original_and_does_not_replace_it(tmp_path, monkeypatch):
    """The restore leg must never become the story of what went wrong.

    Before the retry existed, the restore was one unretried rename with a tiny failure
    window, so the code after it — the refusal record and the re-raise of the ORIGINAL
    content-swap exception — effectively always ran. Giving the restore its own 0.5s budget
    gave it its own way to raise, and an unguarded raise there skips both, so what reaches
    the caller is the restore's exception wearing the original's place. The diagnostic
    surface would then change identity based on whether the restore also hit contention.

    Mutation check: drop the `try/except` around the restore and this fails — `__cause__`
    is `None` and the recorded `failing_operation` set is missing `content_rename_restore`.
    """
    recorded = []
    monkeypatch.setattr(
        publish,
        "_record_publish_swap_refusal",
        lambda exc, **kw: recorded.append(kw.get("failing_operation")),
    )

    dest, staging, prior = tmp_path / "d", tmp_path / "s", tmp_path / "d.prior"
    # `prior` must NOT pre-exist: the aside rename creates it, and a Windows rename
    # onto an existing directory raises FileExistsError before any leg under test runs.
    dest.mkdir(); staging.mkdir()
    original = _oserror(32)
    restore = _oserror(5)

    real = os.rename

    # Three legs, three outcomes, keyed on source — the aside must SUCCEED or the content
    # rename is never reached and the test silently exercises the wrong branch (it did,
    # first time round).
    def renames(src, dst):
        src = Path(src)
        if src == dest:      # aside: dest -> prior
            return real(src, dst)
        if src == staging:   # content: staging -> dest, the original failure
            raise original
        raise restore        # restore: prior -> dest, fails on its own

    monkeypatch.setattr(publish, "_SWAP_RENAME_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(publish.os, "rename", renames)

    with pytest.raises(OSError) as caught:
        publish._swap_publish_staging_entry(dest, staging)

    assert caught.value is restore, "the restore's own failure is what could not complete"
    assert caught.value.__cause__ is original, "the original cause must survive as __cause__"
    assert "content_rename_restore" in recorded
