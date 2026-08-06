"""
test_queue_append_central_root_parity.py — behavioral-parity CHARACTERIZATION test.

Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-queue-append-central-scope-routes-to-claude-klabauter-not-doe.md
(sibling-filed memo that surfaced the legacy CLI's stale doe_root() routing;
this test lands the claude-klabauter-side correction and the regression guard against
future re-divergence).

SCOPE: two live implementations independently decide where `--queue-scope central`
entries land, and they must agree:

  1. coordinator_core/ops/queue_append.py `_output_path` (native op) — CORRECT,
     routes central-scope improvement-queue/lessons writes to claude-klabauter
     unconditionally.
  2. coordinator/bin/coordinator-queue-append `_output_path` (legacy/strangled
     CLI, State-1 fallback per DR-210) — previously routed the same decision to
     the example-doctrine-repo repo via a `doe_root()` call citing a never-ratified plan
     ([example-doctrine-repo] docs/plans/2026-07-06-gate2-w23-state-seam-caller-switch.md,
     `status: draft`, AC1/AC2 `pending`, its own C3 HELD). Fixed to route to
     claude-klabauter, matching (1).

Coverage caveat (Review: code-reviewer — Finding 1): this test calls both
`_output_path` functions DIRECTLY — it never drives a central-scope write
through the live `cc_invoke.route()` transport that decides between the
legacy (State-1) and native (State-2) call sites. No test in this repo
exercises the real State-2 path end-to-end; this test covers only that the
two implementations agree on the routing DECISION when called directly, not
that the transport correctly selects between them. See
`test_coordinator_queue_append.py`'s Test 9a docstring for the companion
caveat on the State-1 side.

Unlike this repo's other `*.test.py` siblings (handoff-category-enum-parity,
pickup-kind-enum-parity), this is NOT a text/enum-regex parity check — the point
here is BEHAVIORAL agreement between two live code paths that each independently
compute a filesystem root from the same routing decision. Both `_output_path`
functions are imported and CALLED directly (the native op is a proper package
module; the legacy CLI has no `.py` extension, so it is loaded via
`importlib.machinery.SourceFileLoader`, matching the file's own shebang/module
shape — `if __name__ == "__main__":` guards `main()` so import alone triggers no
I/O or subprocess calls).

Governing law: docs/wiki/state-placement-law.md § Taxonomy "Central/global state"
routes `state/lessons/` and `state/improvement-queue/` central writes to claude-klabauter
UNCONDITIONALLY. DR-210 (docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md)
forbids deleting the strangled legacy CLI, so a parity test — not deletion — is
what disarms recurrence: the legacy CLI is a State-1 fallback that stays live
indefinitely, and a silent re-divergence on this exact routing decision is what
produced the incident this test closes.

Negative-spec: this test asserts on the RESOLVED ROOT directory only (the
`os.path.dirname(...)` of each function's return value), not the full output
path — the two functions' filename schemes deliberately differ (legacy:
`<date>-<slug>.yaml`; native: content-keyed `<date>-<slug>-<digest12>.yaml`, DR-213
D2(i)). Comparing full paths would fail on filename-scheme divergence that is
NOT the defect this test targets.

Run with: python3 -m pytest coordinator/bin/test_queue_append_central_root_parity.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

_FAKE_CLAUDE_KLABAUTER_ROOT = "/tmp/queue-append-central-root-parity-fake-claude-klabauter-root"
_FAKE_DIGEST12 = "0123456789ab"


def _repo_bin_dir() -> str:
    """Absolute path to the coordinator/bin directory this test lives in."""
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root() -> str:
    """Absolute path to the claude-klabauter repo root (two levels up from bin/)."""
    return os.path.dirname(os.path.dirname(_repo_bin_dir()))


def _legacy_cli_path() -> str:
    return os.path.join(_repo_bin_dir(), "coordinator-queue-append")


def _load_legacy_cli_module():
    """Load coordinator-queue-append as a module via SourceFileLoader.

    The file has no `.py` extension (it is a naked-Python CLI script), so
    `importlib.util.spec_from_file_location` cannot infer a loader from the
    path alone — an explicit `SourceFileLoader` is required. Importing does
    NOT execute `main()` (guarded by `if __name__ == "__main__":`), so no I/O
    or subprocess calls happen at import time.
    """
    path = _legacy_cli_path()
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_queue_append_cli_parity_probe", path
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_native_op_module():
    """Import coordinator_core.ops.queue_append (proper package module)."""
    repo_root = _repo_root()
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import coordinator_core.ops.queue_append as native_module

    return native_module


def test_central_root_parity() -> None:
    """For queue_scope='central', legacy CLI and native op must resolve the SAME
    root directory, for BOTH improvement-queue and lessons schemas.

    Both functions are pointed at the same fake CLAUDE_KLABAUTER_ROOT via env var override
    so the assertion is independent of the actual machine-local registry state —
    this test must pass identically on a fresh clone with no `repos.claude_klabauter`
    entry set.
    """
    name = (
        "coordinator-queue-append._output_path == "
        "coordinator_core.ops.queue_append._output_path (central-scope root)"
    )

    prior_claude_klabauter_root = os.environ.get("CLAUDE_KLABAUTER_ROOT")
    prior_output_root = os.environ.get("QUEUE_APPEND_OUTPUT_ROOT")
    os.environ["CLAUDE_KLABAUTER_ROOT"] = _FAKE_CLAUDE_KLABAUTER_ROOT
    os.environ.pop("QUEUE_APPEND_OUTPUT_ROOT", None)
    try:
        legacy_mod = _load_legacy_cli_module()
        native_mod = _load_native_op_module()

        mismatches: list[str] = []
        for schema_name in ("improvement-queue", "lessons"):
            legacy_path = legacy_mod._output_path(
                schema_name, "parity probe title", queue_scope="central"
            )
            native_path = native_mod._output_path(
                schema_name,
                "parity probe title",
                caller_worktree=None,
                queue_scope="central",
                digest12=_FAKE_DIGEST12,
            )
            legacy_root = os.path.dirname(legacy_path)
            native_root = os.path.dirname(native_path)
            if legacy_root != native_root:
                mismatches.append(
                    f"schema={schema_name!r}: legacy root {legacy_root!r} != "
                    f"native root {native_root!r}"
                )

        if mismatches:
            raise AssertionError(f"{name}: " + ("; ".join(mismatches)))
            return

    finally:
        if prior_claude_klabauter_root is None:
            os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
        else:
            os.environ["CLAUDE_KLABAUTER_ROOT"] = prior_claude_klabauter_root
        if prior_output_root is None:
            os.environ.pop("QUEUE_APPEND_OUTPUT_ROOT", None)
        else:
            os.environ["QUEUE_APPEND_OUTPUT_ROOT"] = prior_output_root


