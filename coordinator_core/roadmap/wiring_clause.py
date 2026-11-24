"""coordinator_core.roadmap.wiring_clause — refuse an exit-criterion statement that names no
wiring path or no runnable check.

"Done" needs a way in (an entry point someone can invoke) and a way to see it work (a check that
runs). Pure in-process regex; no spawns, no I/O, so a named path is not verified to be tracked.
"""

from __future__ import annotations

import re
from typing import Optional

from coordinator_core.roadmap.post_stamp_clause import (
    EXIT_CRITERION_REFUSAL_HEAD,
    REMEDIATION_MARKER,
)

_INVOKE = re.compile(r"\bcoordinator-invoke\s+[a-z][\w.\-]*", re.IGNORECASE)
# A dotted op name (`sizing.accept_exit_criterion`); a file name such as `plan.md` is not one.
_FILE_EXTS = frozenset({
    "py", "md", "json", "yaml", "yml", "sh", "txt", "toml", "ts", "js", "cfg", "ini", "csv", "log",
    "lock", "html", "rs", "cpp", "h",
})
_DOTTED_OP = re.compile(r"(?<![\w./\-])([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)(?![\w/\-])")
# A CLI script under a bin/ or scripts/ directory.
_CLI_PATH = re.compile(r"(?<![\w.\-])(?:[\w.\-]+/)*(?:bin|scripts)/[\w.\-]+\.(?:py|sh)\b")

_BACKTICKED = re.compile(r"`([^`]+)`")
_PYTEST = re.compile(r"\b(?:python3?\s+-m\s+)?pytest\b|\bpython3?\s+-m\s+pytest\b", re.IGNORECASE)
_TEST_PATH = re.compile(r"(?<![\w.\-])(?:[\w.\-]+/)*(?:tests?/[\w.\-/]*|test_[\w.\-]+\.py)")
_TESTS_PASS = re.compile(r"\btests?\b[^.;]*\bpass(?:es|ed)?\b", re.IGNORECASE)

_TEMPLATE = (
    "name the entry point and the check, e.g. "
    "'`coordinator-invoke <op>` <does X>; `python -m pytest <test file>` passes'"
)


def _entry_point(text: str) -> bool:
    if _INVOKE.search(text) or _CLI_PATH.search(text):
        return True
    return any(m.group(1).rsplit(".", 1)[-1] not in _FILE_EXTS for m in _DOTTED_OP.finditer(text))


def _runnable_check(text: str) -> bool:
    if _PYTEST.search(text) or _TEST_PATH.search(text) or _TESTS_PASS.search(text):
        return True
    return any(len(m.group(1).split()) > 1 for m in _BACKTICKED.finditer(text))


def wiring_clause_refusal(statement: object) -> Optional[str]:
    """Refusal message when `statement` lacks a wiring path or a runnable check, else None.

    Entry point: `coordinator-invoke <op>`, a dotted op name, or a `bin/`/`scripts/` CLI path.
    Check: a backticked multi-word command, a pytest invocation or test path, or "tests ... pass".
    The refusal shares `EXIT_CRITERION_REFUSAL_HEAD` / `REMEDIATION_MARKER` with the other
    exit-criterion refusals so remediable-wording consumers keep parsing it.
    """
    text = " ".join(str(statement or "").split())
    missing = []
    if not _entry_point(text):
        missing.append("wiring path (`coordinator-invoke <op>`, an op name or a CLI path)")
    if not _runnable_check(text):
        missing.append("runnable check (a backticked command or a pytest path)")
    if not missing:
        return None
    return (
        f"{EXIT_CRITERION_REFUSAL_HEAD}claim with no {' and no '.join(missing)}; "
        f"{REMEDIATION_MARKER}{_TEMPLATE}."
    )
