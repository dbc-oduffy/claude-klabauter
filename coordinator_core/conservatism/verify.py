"""
coordinator_core.conservatism.verify -- assert that a declared safe direction
actually holds.

Imported by tests only. It is deliberately NOT re-exported from the package
`__init__`: the declaration surface sits on hot paths, the assertion surface
never does.

## Why the control run is not optional by default

A function that raises unconditionally would satisfy a `RAISE` declaration
trivially, and a function that returns its anchor unconditionally would
satisfy `FALL_BACK` trivially. Either passes a test that only exercises the
broken precondition -- and either is a real defect the declaration was
supposed to catch. So `assert_safe_direction_holds` runs `invoke` TWICE: once
with the precondition intact (which must NOT take the safe direction) and
once with it broken (which must). `control=False` opts out only where the
intact path is not deterministic on an arbitrary box, and then the caller
owes a reason in the test.
"""

from __future__ import annotations

from typing import Any, Callable, ContextManager, Type, Union

from coordinator_core.conservatism import (
    SafeDirection,
    declaration_of,
)

__all__ = ["assert_safe_direction_holds"]

# Review: code-reviewer Finding 1 -- a bare `except Exception: return` on a
# RAISE check cannot distinguish the declared refusal from an unrelated bug
# triggered incidentally inside `undeterminable()`. `expect_raises` narrows
# the accepted exception type(s) when the caller supplies it. Left `None` it
# is STILL a legitimate, documented call shape (not silently the old weak
# behaviour): the docstring says so explicitly, and every RAISE call site
# should supply it where the specimen's refusal has a known exception family.


def _matches_anchor(declaration, result: Any) -> bool:
    if declaration.anchor is None:
        return False
    return bool(declaration.anchor(result))


def assert_safe_direction_holds(
    fn: Any,
    *,
    invoke: Callable[[], Any],
    undeterminable: Callable[[], ContextManager[Any]],
    control: bool = True,
    expect_raises: Union[Type[BaseException], "tuple[Type[BaseException], ...]", None] = None,
) -> None:
    """Assert that `fn` resolves the way it declares when its precondition
    cannot be determined.

    `invoke` calls `fn` with whatever arguments its site needs. `undeterminable`
    returns a fresh context manager that makes the precondition undeterminable
    for the duration of the block -- a `monkeypatch.context()`, a patched
    import, a temp directory with the file removed.

    `expect_raises` narrows a `RAISE` site's check to the exception type(s)
    the declared refusal is documented to produce (e.g. `ImportError` for the
    psutil-absence family). Ignored for `FALL_BACK`, which already asserts on
    the returned value rather than on any exception. Left `None`, a `RAISE`
    check falls back to a bare `except Exception`, which cannot distinguish
    the declared refusal from an unrelated bug incidentally triggered inside
    `undeterminable()` -- this is a DELIBERATE, DOCUMENTED weak call shape for
    a site with no single known exception family, not a silently-accepted
    default; supply `expect_raises` wherever the specimen's failure mode is
    known.

    Raises AssertionError naming the site and both directions on failure; the
    message must be readable by someone who has never seen this module.
    """
    declaration = declaration_of(fn)
    if declaration is None:
        raise AssertionError(
            f"{getattr(fn, '__qualname__', fn)!r} declares no safe direction -- "
            "decorate it with declares_safe_direction before verifying it"
        )

    site = getattr(fn, "__qualname__", repr(fn))

    if control:
        try:
            control_result = invoke()
        except Exception as exc:  # noqa: BLE001 - the control run's failure IS the finding
            raise AssertionError(
                f"{site}: control run raised {exc!r} with the precondition intact. "
                "A site that always takes its safe direction is not declaring one -- "
                "either the precondition is broken on this box, or pass control=False with a reason."
            ) from exc
        if declaration.direction is SafeDirection.FALL_BACK and _matches_anchor(declaration, control_result):
            raise AssertionError(
                f"{site}: returned the declared anchor {declaration.anchor!r} with the precondition "
                "INTACT, so the fall-back test below proves nothing. Make the control path return "
                "something distinguishable, or pass control=False with a reason."
            )

    if declaration.direction is SafeDirection.RAISE:
        expected = expect_raises if expect_raises is not None else Exception
        with undeterminable():
            try:
                result = invoke()
            except expected:
                return
            except Exception as exc:  # noqa: BLE001 - the mismatch IS the finding
                raise AssertionError(
                    f"{site}: declares SafeDirection.RAISE ({declaration.because}) but raised "
                    f"{exc!r}, not the expected {expected!r}, with its precondition undeterminable. "
                    "This is either an unrelated bug incidentally triggered by `undeterminable()`, "
                    "or `expect_raises` needs widening to match the site's real refusal."
                ) from exc
        raise AssertionError(
            f"{site}: declares SafeDirection.RAISE ({declaration.because}) but returned {result!r} "
            "with its precondition undeterminable. A silently wrong value here is the harm the "
            "declaration exists to refuse."
        )

    with undeterminable():
        try:
            result = invoke()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"{site}: declares SafeDirection.FALL_BACK to {declaration.anchor!r} "
                f"({declaration.because}) but raised {exc!r} with its precondition undeterminable."
            ) from exc
    if not _matches_anchor(declaration, result):
        raise AssertionError(
            f"{site}: declares SafeDirection.FALL_BACK to {declaration.anchor!r} "
            f"({declaration.because}) but returned {result!r} with its precondition undeterminable."
        )
