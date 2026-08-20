"""
test_coordinator_doc_new_category_flag.py — --category flag + write-time validation
coverage for coordinator-doc-new.

Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md

SCOPE: covers the two changes the source memo's incident named as root cause:

  1. coordinator-doc-new had no --category argument, so a caller could not set
     the category: frontmatter value at scaffold time and had to scaffold a
     known-wrong literal then hand-edit. Fixed by threading an optional
     --category through the six handoff-schema-family scaffolders
     (handoff, recovery, spinoff, roadmap-baton, goal-seed,
     roadmap-seed).
  2. coordinator-doc-new never validated its own emitted category value, so a
     bad category was written freely and only surfaced much later, at
     archive-stamp-cli verb time, in a different session. Fixed by
     _validate_category, called from every one of the six scaffolders (so it
     guards each type's own hardcoded default, not just the --category path).

This test invokes the real CLI as a subprocess (matching
test_coordinator_doc_new_emitter_parity.py's convention) rather than importing
coordinator-doc-new as a module — it has no .py extension and is not meant to
be imported.

Run with: python3 -m pytest coordinator/bin/test_coordinator_doc_new_category_flag.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# One handoff-schema-family type per each of the six scaffolders' distinct
# hardcoded default, plus required-arg shims so the invocation reaches the
# scaffolder's own dispatch branch rather than dying on an earlier, unrelated
# required-arg check (mirrors _required_args_for in the emitter-parity test).
_TYPES_AND_DEFAULT_CATEGORY: dict[str, str] = {
    "handoff": "infra",
    "recovery": "infra",
    "spinoff": "infra",
    "roadmap-baton": "roadmap",
    "goal-seed": "infra",
    "roadmap-seed": "roadmap",
}

_REQUIRED_ARGS: dict[str, list[str]] = {
    # --no-sizing-object joins the required-arg shims: roadmap-baton is held to
    # the same explicit-sizing-answer bar as --type plan. This suite tests the
    # --category axis, so it declares absence rather than minting a sizing object.
    "roadmap-baton": [
        "--roadmap-id", "smoke-rm", "--stub-id", "smoke-rm-1", "--no-sizing-object",
    ],
}


def _repo_bin_dir() -> str:
    """Absolute path to the coordinator/bin directory this test lives in."""
    return os.path.dirname(os.path.abspath(__file__))


def _cli_path() -> str:
    return os.path.join(_repo_bin_dir(), "coordinator-doc-new.py")


def _invoke(doc_type: str, out_path: str, extra_args: list[str]) -> tuple[int, str]:
    """Invoke coordinator-doc-new --type <doc_type>, returning (returncode,
    combined stdout+stderr). Uses the same interpreter running this test
    (sys.executable) rather than relying on PATH resolution.
    """
    cmd = [
        sys.executable,
        _cli_path(),
        "--type",
        doc_type,
        "--title",
        "smoke",
        "--out",
        out_path,
        *_REQUIRED_ARGS.get(doc_type, []),
        *extra_args,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def test_default_category_unchanged_when_flag_omitted() -> None:
    """Each of the six scaffolders keeps emitting ITS CURRENT literal default
    (behavior-preserving) when --category is not passed — the fix must not
    unify all six onto one value.
    """
    with tempfile.TemporaryDirectory(prefix="coordinator-doc-new-category-flag-") as tmpdir:
        for doc_type, expected_default in _TYPES_AND_DEFAULT_CATEGORY.items():
            name = f"--type {doc_type} (no --category): defaults to category: {expected_default}"
            out_path = os.path.join(tmpdir, f"{doc_type}-default.md")
            try:
                rc, combined = _invoke(doc_type, out_path, [])
            except subprocess.TimeoutExpired:
                raise AssertionError(f"{name}: " + ("scaffold invocation timed out after 60s"))
            if rc != 0:
                raise AssertionError(f"{name}: " + (f"exited {rc}, expected 0. Output: {combined.strip()[:500]}"))
            if not os.path.isfile(out_path):
                raise AssertionError(f"{name}: " + (f"scaffold exited 0 but {out_path} was not written"))
            with open(out_path, "r", encoding="utf-8") as f:
                body = f.read()
            if f"category: {expected_default}" not in body:
                raise AssertionError(f"{name}: " + (f"expected literal 'category: {expected_default}' in output; not found. "
                    f"Output head: {body[:300]!r}"))


def test_explicit_category_flag_is_honored() -> None:
    """--category <value> is threaded through to the emitted frontmatter for
    every handoff-schema-family type, overriding that type's own default.
    """
    with tempfile.TemporaryDirectory(prefix="coordinator-doc-new-category-flag-") as tmpdir:
        for doc_type in _TYPES_AND_DEFAULT_CATEGORY:
            name = f"--type {doc_type} --category docs: emits category: docs"
            out_path = os.path.join(tmpdir, f"{doc_type}-explicit.md")
            try:
                rc, combined = _invoke(doc_type, out_path, ["--category", "docs"])
            except subprocess.TimeoutExpired:
                raise AssertionError(f"{name}: " + ("scaffold invocation timed out after 60s"))
            if rc != 0:
                raise AssertionError(f"{name}: " + (f"exited {rc}, expected 0. Output: {combined.strip()[:500]}"))
            with open(out_path, "r", encoding="utf-8") as f:
                body = f.read()
            if "category: docs" not in body:
                raise AssertionError(f"{name}: " + (f"expected literal 'category: docs' in output; not found. "
                    f"Output head: {body[:300]!r}"))


def test_unknown_category_fails_loud_naming_legal_values() -> None:
    """An out-of-enum --category value hard-fails (nonzero exit) BEFORE any
    file is written, with a stderr message naming all legal values — the
    exact failure mode the source memo's incident asked for, so a bad value
    can no longer reach disk to be picked up unvalidated downstream.
    """
    legal_values = ("roadmap", "infra", "bug", "docs", "research", "refactor",
                     "uncategorized", "queue-derived-baton")
    with tempfile.TemporaryDirectory(prefix="coordinator-doc-new-category-flag-") as tmpdir:
        for doc_type in _TYPES_AND_DEFAULT_CATEGORY:
            name = f"--type {doc_type} --category feature: fails loud, names legal values"
            out_path = os.path.join(tmpdir, f"{doc_type}-bad.md")
            try:
                rc, combined = _invoke(doc_type, out_path, ["--category", "feature"])
            except subprocess.TimeoutExpired:
                raise AssertionError(f"{name}: " + ("scaffold invocation timed out after 60s"))
            if rc == 0:
                raise AssertionError(f"{name}: " + ("exited 0 for an out-of-enum --category value; expected nonzero"))
            if os.path.isfile(out_path):
                raise AssertionError(f"{name}: " + (f"{out_path} was written despite an invalid --category — must fail before write"))
            missing = [v for v in legal_values if v not in combined]
            if missing:
                raise AssertionError(f"{name}: " + (f"error output does not name all legal values; missing: {missing}. "
                    f"Output: {combined.strip()[:500]}"))

