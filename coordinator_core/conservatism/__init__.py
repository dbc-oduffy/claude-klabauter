"""
coordinator_core.conservatism -- declare, at the call site, which direction a
function fails when a precondition cannot be determined.

Purpose: the discipline "when you cannot determine whether a precondition
holds, resolve toward the cautious answer" is load-bearing in at least five
independent places in this fleet and is a CONVENTION in all of them -- a
docstring sentence, an anchor constant, a comment. A convention is not
checkable, so nothing fails when a later edit quietly flips a direction.
This module makes the direction a machine-readable declaration and
`coordinator_core.conservatism.verify` makes it assertable.

## There is no single safe direction, and imposing one would break working code

`concurrency_probe.default_usable_ram_gb` RAISES when psutil is missing --
there is no safe fallback for a RAM figure and a silently wrong worker cap
defeats the escape-hatch contract. Its neighbour `default_physical_cores`
FALLS BACK to `os.cpu_count()` -- a wrong core count is off by ~2x and
survivable. Both are correct. A primitive that could not express both would
be the wrong primitive, so the direction is declared PER SITE and this
module never supplies a default.

## Vocabulary

This module says SAFE DIRECTION, with exactly two values: `RAISE` and
`FALL_BACK` (to a declared anchor).

`RAISE`/`FALL_BACK` are not a synonym for "fail-open"/"fail-closed" --
"fail-open"/"fail-closed" keep their ordinary senses here. Open/closed names
WHICH WAY a site resolves; RAISE/FALL_BACK name WHAT IT DOES, which is the
thing a test has to assert and the thing a call site has to implement.

This module was specified to supersede a vocabulary collision and does not,
because the collision was resolved upstream while it was being written:
`_posture.py`'s anchor was named `_FAIL_OPEN_POSTURE` while denoting the most
CAUTIOUS value, and DoE-claude renamed it to `_MOST_CAUTIOUS_POSTURE` at
`4026b4250`. No term of theirs is superseded by a term of ours. The rename is
recorded here rather than dropped because the old name was not describing
nothing -- posture resolution genuinely does fail open; the name was describing
the PATH while sitting on the VALUE that path selects, which is what made it
read backwards, and that is the mistake worth not repeating.

## Shapes rejected, and why (recorded so they are not re-litigated silently)

- **A wrapping decorator** that enforces the direction at runtime -- catches
  the same argument, then adds a call frame and a try/except to every
  declaring site, including hot ones, and CHANGES BEHAVIOUR at migration.
  This baton's contract was that no existing fail direction moves; a wrapper
  cannot make that promise by construction, only by review.
- **A `Result`/`Option` return type.** It is the strongest shape in the
  literature (the corpus grounds it well) and it is unmigratable here: it
  changes every caller of every specimen at once, which is the opposite of
  migrating specimens one at a time with a test each. It also cannot express
  `_posture.py`'s site at all, since that file is not ours to change.
- **A single global fail direction** -- see the section above; it would have
  broken `default_physical_cores` or `default_usable_ram_gb`, whichever way
  it was set.

What survived is the cheapest shape that is still checkable: a marker plus an
assertion helper. It borrows the shape this repo already runs on --
`coordinator_core.benchmarks.declare_benchmark_origin` plus
`tests/test_benchmark_drivers_declare_origin.py` -- rather than importing a
new one.

## Counter-evidence carried deliberately

Failing cautiously is not free. The research corpus for this work
(`state/roadmap/cloud-em-2026-09-06/research-corpus/fail-closed-defaults-as-one-primitive.md`)
found real availability harms from reflexive fail-closed: partition
exploitation, cascading refusal amplification, and convention drift under
operational pressure. An unattended cloud run is exactly where a cascading
conservative refusal is least visible. A primitive that makes one direction
cheap makes its failure cheap too -- which is why `declares_safe_direction`
requires a `because` string naming the harm each direction trades against,
and why it has no default value for `direction`.

## A malformed declaration takes its whole module import down with it

`declares_safe_direction` validates eagerly, at decoration time -- i.e. at the
declaring MODULE's import time, not at the declared function's call time.
This is deliberate (see the validation itself, above), but it carries a
property worth naming for whoever next touches a declaring call site: a
future edit that breaks validation (a typo turning `anchor=` into `anchors=`,
leaving a `FALL_BACK` anchor-less) fails the entire module's import, not just
the mis-declared function. Every live decoration today passes valid
arguments, so this is a property of the design, not a defect in any current
call site. (Review: code-reviewer Finding 4.)

## Cost

This is a MARKER, not a wrapper. `declares_safe_direction` sets one
attribute and returns the SAME function object -- no closure, no extra call
frame, no import beyond `enum`/`dataclasses`. It changes no behaviour at
any declaring site, by construction, so it cannot move a guard hot path
past the 500ms brightline (DR-344). The assertion machinery lives in the
sibling `verify` module, imported only by tests.
"""

from __future__ import annotations

from enum import Enum

# Deliberately NOT `dataclasses` and NOT `typing`. Measured on this box:
# importing `dataclasses` costs 9.3ms (it pulls `inspect`) and `typing` 2.4ms,
# against 1.1ms for this module's own body -- a 12x tax for one frozen record
# and three annotations. `enum` is already resolved by interpreter start and is
# free. A primitive meant to be cheap enough for a guard hot path (DR-344:
# 500ms end-to-end, <50ms to reach a warm engine) does not get to spend 12ms on
# ergonomics. Annotations below are strings by `from __future__ import
# annotations`, so no runtime typing import is needed to carry them.

__all__ = [
    "SafeDirection",
    "SafeDirectionDeclaration",
    "declares_safe_direction",
    "declaration_of",
    "iter_declarations",
]

_ATTR = "__safe_direction__"

class SafeDirection(Enum):
    """The two directions a site can resolve toward when it cannot determine
    its precondition. See the module docstring for why there is no third
    value and no default."""

    RAISE = "raise"
    """Refuse to return a value at all. Correct where a wrong answer has
    unbounded error and a caller cannot tell a wrong answer from a right
    one."""

    FALL_BACK = "fall_back"
    """Return a declared anchor -- the value whose behaviour is known and
    survivable. Correct where the degraded answer is bounded-wrong and the
    caller keeps working."""


class SafeDirectionDeclaration:
    """What a declaring site asserts about itself. `anchor` is meaningful
    only for `FALL_BACK`: a predicate `(result) -> bool` accepting the
    degraded results. `None` for `RAISE`, which has no anchor.

    Immutable by convention (`__slots__`, no setters exercised) rather than by
    `@dataclass(frozen=True)` -- see the import note above for the cost."""

    __slots__ = ("direction", "because", "anchor")

    def __init__(self, direction, because, anchor=None):
        self.direction = direction
        self.because = because
        self.anchor = anchor

    def __repr__(self) -> str:
        return (
            f"SafeDirectionDeclaration(direction={self.direction!r}, "
            f"because={self.because!r}, anchor={self.anchor!r})"
        )


def declares_safe_direction(
    direction: "SafeDirection",
    *,
    because: str,
    anchor=None,
):
    """Mark `fn` with the direction it resolves toward when its precondition
    is undeterminable. Returns the same function object; wraps nothing.

    `because` is required and must name what the OTHER direction would have
    cost -- a declaration that only restates the direction is not evidence
    the tradeoff was made.

    A `FALL_BACK` declaration must carry an `anchor`; a `RAISE` declaration
    must not. Both are enforced here, at import, rather than in the test --
    a malformed declaration is a defect in the site, not in its coverage.
    """
    if not isinstance(direction, SafeDirection):
        raise TypeError(f"declares_safe_direction: direction must be a SafeDirection, got {direction!r}")
    if not because or not because.strip():
        raise ValueError("declares_safe_direction: `because` must name the harm the rejected direction carries")
    if direction is SafeDirection.FALL_BACK and anchor is None:
        raise ValueError("declares_safe_direction: FALL_BACK must declare the anchor it degrades to")
    if direction is SafeDirection.FALL_BACK and not callable(anchor):
        # Review: code-reviewer -- a non-callable, non-None anchor passed the
        # `is None` check above and then raised a bare TypeError at first
        # assertion, not at import, contradicting this function's own
        # docstring claim that malformed declarations are enforced here.
        raise ValueError("declares_safe_direction: FALL_BACK anchor must be callable")
    if direction is SafeDirection.RAISE and anchor is not None:
        raise ValueError("declares_safe_direction: RAISE has no anchor -- it returns no value")

    declaration = SafeDirectionDeclaration(direction=direction, because=because, anchor=anchor)

    def _mark(fn):
        setattr(fn, _ATTR, declaration)
        return fn

    return _mark


def declaration_of(fn) -> "SafeDirectionDeclaration | None":
    """The declaration on `fn`, or None if it declares nothing."""
    return getattr(fn, _ATTR, None)


def iter_declarations(module):
    """Yield `(qualified_name, declaration)` for every declaring callable in
    `module`, so a meta-test can refuse an undeclared-but-untested site.

    Scoped to the ONE module passed in -- not a package walk. A declaring
    site in a module this meta-test does not name is unguarded by this
    mechanism. (Review: overengineering-reviewer -- documented rather than
    widened; see `tests/test_declared_directions_hold.py`'s specimen
    census for what is covered today.)

    A site IMPORTED into `module` is attributed to the module that defines it,
    not to this one: a consumer that imports a declaring helper has not made a
    declaration, and yielding it under the importer's name would report a site
    that does not exist there and demand a second assertion for one function."""
    for name in dir(module):
        candidate = getattr(module, name, None)
        declaration = declaration_of(candidate)
        if declaration is None:
            continue
        owner = getattr(candidate, "__module__", module.__name__)
        if owner != module.__name__:
            continue
        yield f"{module.__name__}.{name}", declaration
