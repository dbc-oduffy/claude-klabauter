"""
coordinator_core.testing.golden — shared committed-fixture ("golden") helper for
freezing external-oracle differential parity suites off their live dependency.

Purpose: several of claude-klabauter's parity suites (test_dag_js_parity.py and siblings) diff
Claude-klabauter's native port **live** against a coordinator-claude JS oracle at test time, skipping
cleanly whenever node or the oracle checkout is absent. That skip-on-missing shape is
a silent-green hazard: the moment coordinator-claude deletes the oracle `.js` it no longer needs,
Claude-klabauter's "correctness proof" for the ported hub evaporates while the suite keeps
reporting green. This module lets a suite capture the oracle's output **once** into a
committed fixture ("golden") and diff the native port against that fixture forever
after — no node/oracle needed at ordinary test-run time.

Port source: none — net-new (2026-07-21 de-node Gate A).
Spec backlink: pln-freeze-the-6-node-oracle-parit-6a21ab § C0

Usage (the pattern every parity-suite conversion in this sweep copies):
    from coordinator_core.testing.golden import assert_matches_golden, is_capturing

    def test_some_case(...):
        if is_capturing():
            oracle_out = _run_node(...)               # only invoked during capture
            actual = _normalize(oracle_out.stdout)
        else:
            native_out = _run_python(...)              # the only thing that runs
            actual = _normalize(native_out.stdout)      # ordinarily (no node needed)
        assert_matches_golden(actual, "my_suite_namespace", "some_case", kind="text")

Goldens resolve to `<dir-containing-the-calling-test-file>/_goldens/<namespace>/
<case>.json` (kind="json") or `<case>.txt` (kind="text") — resolved by walking the
call stack for the nearest frame outside this module, so callers never pass their own
`__file__` explicitly. Regenerate deliberately via `CAPTURE_GOLDENS=1 python3 -m
pytest <suite>` (see `is_capturing()`); anything else that touches a missing golden
raises `GoldenMissingError`.

Negative-spec: does NOT ever `pytest.skip` on a missing golden, on a `kind` mismatch,
or on any other resolution failure — that reproduces exactly the silent-green hazard
this module exists to close. A missing/unreadable golden is always a hard failure
(`GoldenMissingError` or a plain exception), never a skip. Does NOT infer `kind` from
whichever fixture file happens to exist on disk — the caller states `kind` explicitly,
so a suite can never silently read the wrong extension's stale fixture.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any, Union

_CAPTURE_ENV_VAR = "CAPTURE_GOLDENS"
_KIND_EXTENSIONS = {"text": "txt", "json": "json"}


class GoldenMissingError(Exception):
    """Raised when a golden fixture file is absent (and capture mode is not on).

    Hard failure by design — see module docstring negative-spec. Callers must never
    catch this and downgrade it to `pytest.skip`.
    """


def is_capturing() -> bool:
    """True iff CAPTURE_GOLDENS=1 is set — the documented fixture-(re)generation path."""
    return os.environ.get(_CAPTURE_ENV_VAR) == "1"


def _resolve_goldens_dir() -> Path:
    """Walk the call stack for the nearest frame outside this module and return the
    `_goldens/` directory beside that frame's file.

    This is how `load_golden`/`assert_matches_golden` resolve "the calling test
    suite's own directory" without requiring every call site to pass `__file__`
    explicitly. Frames belonging to this module itself (e.g. `assert_matches_golden`
    calling `load_golden` internally) are skipped, so the resolution is correct
    regardless of how many golden.py-internal layers sit between the real caller and
    this function.
    """
    this_file = Path(__file__).resolve()
    for frame_info in inspect.stack():
        candidate = Path(frame_info.filename).resolve()
        if candidate != this_file:
            return candidate.parent / "_goldens"
    raise RuntimeError("coordinator_core.testing.golden: could not resolve a caller file outside golden.py")


def _ext_for_kind(kind: str) -> str:
    try:
        return _KIND_EXTENSIONS[kind]
    except KeyError:
        raise ValueError(
            f"coordinator_core.testing.golden: unknown kind {kind!r} (expected one of "
            f"{sorted(_KIND_EXTENSIONS)!r})"
        ) from None


def _golden_path(namespace: str, case: str, kind: str) -> Path:
    return _resolve_goldens_dir() / namespace / f"{case}.{_ext_for_kind(kind)}"


def load_golden(namespace: str, case: str, *, kind: str = "text") -> Union[bytes, Any]:
    """Load a committed golden fixture.

    kind="text" returns the fixture's raw bytes. kind="json" returns the parsed
    object. Raises `GoldenMissingError` (never `pytest.skip`) if the fixture file is
    absent — see module docstring negative-spec.
    """
    path = _golden_path(namespace, case, kind)
    if not path.is_file():
        raise GoldenMissingError(
            f"golden fixture missing: {path} (namespace={namespace!r}, case={case!r}, "
            f"kind={kind!r}). Run the suite with CAPTURE_GOLDENS=1 to (re)generate it "
            "deliberately — see coordinator_core/testing/golden.py module docstring."
        )
    raw = path.read_bytes()
    if kind == "json":
        return json.loads(raw.decode("utf-8"))
    return raw


def _write_golden(namespace: str, case: str, content: Any, kind: str) -> Path:
    path = _golden_path(namespace, case, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "json":
        obj = json.loads(content) if isinstance(content, (str, bytes)) else content
        raw = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    else:
        if isinstance(content, bytes):
            raw = content
        elif isinstance(content, str):
            raw = content.encode("utf-8")
        else:
            raise TypeError(
                f"coordinator_core.testing.golden: text-kind content must be str or "
                f"bytes, got {type(content)!r}"
            )
    path.write_bytes(raw)
    return path


def assert_matches_golden(actual: Any, namespace: str, case: str, *, kind: str = "text") -> None:
    """Assert `actual` matches the committed golden for (namespace, case).

    kind="text": exact byte comparison (`actual` may be `str` or `bytes`).
    kind="json": normalized comparison — both sides are compared as parsed Python
    objects, so key ordering / incidental whitespace differences never false-fail.

    Under `CAPTURE_GOLDENS=1` (see `is_capturing()`), this WRITES `actual` as the new
    golden instead of asserting — the documented, deliberate regeneration path. This
    is the only way a golden fixture is ever created or updated.
    """
    if is_capturing():
        _write_golden(namespace, case, actual, kind)
        return

    expected = load_golden(namespace, case, kind=kind)
    if kind == "json":
        actual_obj = json.loads(actual) if isinstance(actual, (str, bytes)) else actual
        assert actual_obj == expected, (
            f"golden mismatch for {namespace}/{case} (json):\n"
            f"  actual:   {actual_obj!r}\n"
            f"  expected: {expected!r}"
        )
    else:
        actual_bytes = actual.encode("utf-8") if isinstance(actual, str) else actual
        assert actual_bytes == expected, (
            f"golden mismatch for {namespace}/{case} (text):\n"
            f"  actual:   {actual_bytes!r}\n"
            f"  expected: {expected!r}"
        )
