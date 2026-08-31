"""`node --test` is a runner, and the guard could not see it at all.

Purpose: `node` was absent from `_RUNNER_PREFILTER_RE`, so NO `node --test`
shape reached this guard -- not the classification leg, not the Layer-3
subagent identity deny, not the Tier-F/U grant check. `check()` returns
early for any command the prefilter misses, so all three legs were bypassed
by one omission. Identical class to the 2026-08-03 `tox`/`nox` gap
(`_classify_tox_nox`'s docstring records it), and reported as
`cross-repo/archive/2026-08-11-doe-claude-em-tier-u-node-runner-
unclassified.md`.

THE RULING THIS FILE PINS (DR-395). The reporting memo asked one question --
classifier gap, or ceremony prose overreaching? -- and it had two answers,
because it conflated two claims:

  * The shapes the guard could NOT SEE (`node --test` bare, `node --test .`,
    `node --test <a testpaths root>`) are unscoped runner invocations and
    Tier U by DR-088's disjunct. The CLASSIFIER was wrong. Fixed.
  * The shape the memo actually named, `node --test <path>/run.js`, has a
    positional naming a FILE, so it is scoped by the identical predicate
    that permits `pytest tests/test_one.py`. The PROSE was wrong. Not fixed
    here -- it is the reporting repo's surface, and their correction.

So this is one ruling per claim, not "fix both sides to agree", which is
what the baton criterion forbade.

Negative-spec: this file pins CLASSIFICATION only -- which shapes are
suite-shaped. It asserts nothing about what `check()` then does with that
verdict (deny vs grant-check vs allow-for-the-EM), which is the identity and
grant legs' business and is covered by
`test_check_test_suite_invocation.py`. It also does not shell out to `node`;
the classifier is a pure function of argv and the configured testpaths, and
`node` need not be installed for any of this to be decidable.
"""

from __future__ import annotations

import shlex
from typing import List, Sequence, Tuple

import pytest

from coordinator_core.bash_guards.check_test_suite_invocation import (
    _RUNNER_PREFILTER_RE,
    _classify_tokens,
    _is_node_scope,
)

#: Stand-in for a repo whose configured roots include a `tests` directory --
#: supplied explicitly so these cases do not depend on this repo's own
#: pyproject, which would make the suite-shaped rows pass for the wrong
#: reason the day someone edits `testpaths`.
_TESTPATHS: Tuple[str, ...] = ("coordinator_core", "coordinator/tests", "tests")


def _classify(cmd: str) -> "str | None":
    return _classify_tokens(shlex.split(cmd), _TESTPATHS, None)


def test_node_is_in_the_runner_prefilter() -> None:
    """The whole defect was an absence here, so it gets its own assertion.

    Without this the guard returns before any leg runs, and every case below
    would pass vacuously against a classifier nothing ever calls."""
    assert _RUNNER_PREFILTER_RE.search("node --test")


_SUITE_SHAPED: List[str] = [
    "node --test",
    "node --test --watch",
    "node --experimental-test-coverage --test",
    "node --test .",
    "node --test ./",
    "node --test tests",
    "node --test tests/",
    "node --test coordinator_core",
]


@pytest.mark.parametrize("cmd", _SUITE_SHAPED, ids=_SUITE_SHAPED)
def test_unscoped_node_test_shapes_are_suite_shaped(cmd: str) -> None:
    """An unscoped runner invocation is Tier U by DR-088's disjunct, and by
    DoE's own R1 ruling that tier is a property of the invocation's SHAPE
    rather than of the config key it was read from.

    A testpaths ROOT is included deliberately: `node --test tests` selects
    the whole configured suite while wearing a scope's clothing, which is
    the case `_is_real_scope` exists to reject."""
    assert _classify(cmd) == "node --test"


_SCOPED: List[str] = [
    "node --test coordinator/tests/plugin-ecosystem/run.js",
    "node --test x.test.js",
    "node --test ./a.mjs",
    "node --test sub/dir/b.cjs",
    "node --test a.ts",
]


@pytest.mark.parametrize("cmd", _SCOPED, ids=_SCOPED)
def test_file_scoped_node_test_shapes_are_permitted(cmd: str) -> None:
    """A positional naming a file is scope, by the same predicate that
    permits `pytest tests/test_one.py`."""
    assert _classify(cmd) is None


def test_plain_node_is_not_a_test_runner() -> None:
    """`--test` is the flag that makes node a runner, so it is the flag that
    makes this leg apply. Without it this guard would fire on every node
    invocation in the fleet -- a build script, a codegen step, a one-off."""
    assert _classify("node run.js") is None
    assert _classify("node server.js --port 3000") is None
    assert _classify("node -e 'console.log(1)'") is None


def test_the_reported_invocation_stays_permitted_and_that_is_the_ruling() -> None:
    """The memo's own case, pinned as a DECISION rather than left to drift.

    `2026-08-11-doe-claude-em-tier-u-node-runner-unclassified.md` reported
    `node --test <path>/run.js` as allowed and asked whether that was the
    classifier's gap or their prose overreaching. Ruled: their prose. The
    operand names a file, so it is scoped by the uniform predicate.

    The unboundedness the memo was reaching for lives INSIDE run.js, which
    fans out to a whole plugin ecosystem -- invisible to a text classifier,
    and unreachable by any filename heuristic that would not also deny the
    genuinely-single-file `node --test x.test.js` pinned above. If this
    assertion is ever flipped, the two rows it would break are that one and
    this one, and the ruling in DR-395 has to be revisited first."""
    assert _classify("node --test coordinator/tests/plugin-ecosystem/run.js") is None


def test_node_scope_predicate_never_re_admits_a_testpaths_root() -> None:
    """`_is_node_scope` adds a JS/TS-suffix arm on top of `_is_real_scope`.

    The arm must not become an escape hatch: a testpaths root that happens
    to end in a Node suffix is still the whole suite, because the testpaths
    checks run before the suffix arm."""
    testpaths: Sequence[str] = ("bundle.js", "coordinator_core")
    assert _is_node_scope("bundle.js", testpaths, None) is False
    assert _is_node_scope("other.js", testpaths, None) is True
    assert _is_node_scope(".", testpaths, None) is False
    assert _is_node_scope("..", testpaths, None) is False


def test_suffix_arm_is_case_insensitive() -> None:
    """A capitalised suffix is the same file on Windows, which is
    first-class in this repo."""
    assert _is_node_scope("A.MJS", _TESTPATHS, None) is True
    assert _is_node_scope("Test.JS", _TESTPATHS, None) is True
