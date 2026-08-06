#!/usr/bin/env python3
"""
test_coordinator_doc_new_emitter_parity.py — generic manifest-vs-emitter parity test.

Spec backlink: cross-repo/inbox/2026-07-19-example-market-data-repo-em-strategic-self-description-scaffold-emitter-missing.md
(the incident this test generalizes past): a manifest-registered `--type
strategic-self-description` had NO body-skeleton emitter branch in
coordinator-doc-new's `main()` dispatch chain — invoking it crashed at the
`raise AssertionError(f"unreachable doc_type: {doc_type!r}")` backstop
(coordinator-doc-new:3066). That specific type has since been fixed. This test
exists so the NEXT doc-type addition can't reintroduce the same gap silently:
it is a GENERIC, type-set-driven regression net, not a type-specific smoke.

SCOPE: for every doc type coordinator-doc-new recognizes as `--type`-valid
(sourced from the manifest via coordinator_registry, exactly as the CLI itself
sources it — see `_known_types()` below), invoke the scaffold with the minimal
common arg shape (`--type <T> --title smoke --out <tmpfile>`) PLUS whatever
type-specific required args `_required_args_for()` supplies (memo's
`--to`/`--topic`, sidecar types' `--plan`, run-report's `--plan`/`--chunk`,
audit-record's `--system`, review-findings' `--slice`/`--scope`) and assert the
combined stdout+stderr does NOT contain the literal string "unreachable
doc_type". This isolates the MISSING-EMITTER-BRANCH defect class from every
other possible failure mode:

  - Review: code-reviewer Finding 2 — a type's required-arg validation check
    (e.g. `if doc_type == "audit-record": if not args.system: ...`) is a
    SEPARATE, independently-gated code block from its actual `elif doc_type ==
    "audit-record": ...` dispatch branch three-hundred-odd lines later; merely
    reaching the former proves nothing about whether the latter still exists.
    `_required_args_for()` supplies valid values for every required-arg-gated
    type so the invocation actually clears required-arg validation and reaches
    the type's own dispatch branch (or the `else:` guard) — that is what this
    test now proves, not just "recognized and routed to its own validation
    branch". A type that still errors on something else downstream (e.g. a
    filesystem/git precondition) still PASSES this test — only the
    "unreachable doc_type" backstop string is a failure; this test does not
    attempt full end-to-end scaffold correctness for every type.
  - A type that scaffolds cleanly (exit 0) also passes.
  - A type that delegates to a sibling CLI (queue types via
    _delegate_to_queue_append, `lesson` via _delegate_to_lesson_promote,
    `workflow` via _delegate_to_workflow_scaffold) exits via that sibling
    process's own argparse/exit path before ever reaching this CLI's
    known-type gate or dispatch chain — so it structurally cannot emit
    "unreachable doc_type" from THIS CLI. These are asserted the same way as
    every other type (no skip-list needed): the assertion (no substring match)
    holds trivially for them, and asserting it directly is stronger evidence
    than assuming it.
  - The ONLY way to fail this test is a doc type that is manifest-known
    (`--type` accepted, not rejected as "unknown --type") but for which
    `main()`'s dispatch chain has no branch — i.e. exactly the defect class
    this test exists to catch.

NEGATIVE-SPEC: this test does NOT hardcode the known-types list. It imports
`coordinator_registry.KNOWN_TYPES` (parsed at coordinator_registry import time
from schemas/coordinator-registry.manifest.json) and unions in the local
"run-report" shim exactly as coordinator-doc-new's own `_KNOWN_TYPES` does (the
local run-report shim near the `_KNOWN_TYPES` union) — so a future doc-type
addition to the manifest is picked up automatically the next time this test
runs, without editing this file. If a future type genuinely cannot be
smoke-invoked this way (e.g. it requires network access or destructive side
effects), it must be added to `_EXPLICIT_SKIPS` below WITH a logged reason
printed at test time — silent skips are not permitted.

Run with: python3 -m pytest coordinator/bin/test_coordinator_doc_new_emitter_parity.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_UNREACHABLE_MARKER = "unreachable doc_type"


# Types that must NOT be smoke-invoked via this test's minimal-args shape,
# each with a logged reason. Empty by design — every currently-known type
# has been verified safe to invoke with `--title smoke --out <tmpfile>`
# (delegated types fail fast via the sibling CLI's own argparse; sidecar and
# audit/memo/run-report types fail fast on their own required-arg checks).
# Add an entry here ONLY when a future type cannot be smoke-invoked safely
# (e.g. it performs a genuinely destructive or network-dependent action even
# on required-arg failure) — never as a way to silence a real emitter gap.
_EXPLICIT_SKIPS: dict[str, str] = {}


def _repo_bin_dir() -> str:
    """Absolute path to the coordinator/bin directory this test lives in."""
    return os.path.dirname(os.path.abspath(__file__))


def _cli_path() -> str:
    return os.path.join(_repo_bin_dir(), "coordinator-doc-new")


def _known_types() -> frozenset[str]:
    """Return the effective --type set, sourced identically to the CLI itself.

    Mirrors coordinator-doc-new's own construction (the local run-report shim near
    the `_KNOWN_TYPES` union): coordinator_registry.KNOWN_TYPES (manifest-derived:
    docTypes ∪ queueTypes) unioned with the local "run-report" shim, which is not
    yet manifest-registered (see coordinator-doc-new's inline comment on
    _KNOWN_TYPES for the C8a gate this shim is waiting on). Deliberately does NOT
    hardcode a literal type list — the entire point of this test is to track the
    manifest, not a snapshot of it.
    """
    lib_dir = os.path.join(_repo_bin_dir(), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import coordinator_registry  # noqa: E402  (import after sys.path mutation, by design)

    return coordinator_registry.KNOWN_TYPES | frozenset({"run-report"})


def _sidecar_types() -> frozenset[str]:
    """Return coordinator_registry.SIDECAR_TYPES (isSidecar doc types)."""
    lib_dir = os.path.join(_repo_bin_dir(), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import coordinator_registry  # noqa: E402  (import after sys.path mutation, by design)

    return coordinator_registry.SIDECAR_TYPES


def _required_args_for(doc_type: str) -> list[str]:
    """Per-type minimal-valid required-args, so the invocation reaches the type's
    own dispatch `elif` (or the `else:` guard) instead of dying at an earlier,
    unrelated required-arg validation `sys.exit(1)`.

    Review: code-reviewer Finding 2 — the prior minimal-args-only invocation never
    exercised the dispatch chain for any required-arg-gated type; reaching the
    required-arg check is a SEPARATE code block from the actual `elif` dispatch
    branch and proves nothing about the latter still existing. Values below are
    hand-picked to satisfy each type's parse-time validation (slug regexes,
    denylists) exactly, per coordinator-doc-new's own required-arg blocks.
    """
    if doc_type == "memo":
        return ["--to", "smoke-em", "--topic", "smoke"]
    if doc_type in _sidecar_types():
        return ["--plan", "smoke-plan"]
    if doc_type == "run-report":
        return ["--plan", "tasks/smoke/plan.md", "--chunk", "smoke-chunk"]
    if doc_type == "audit-record":
        return ["--system", "smoke-sys"]
    if doc_type == "review-findings":
        return ["--slice", "smoke", "--scope", "smoke.py"]
    return []


def _invoke_scaffold(doc_type: str, out_path: str) -> tuple[int, str]:
    """Invoke coordinator-doc-new --type <doc_type> with minimal common args plus
    whatever type-specific required args _required_args_for() supplies, so the
    invocation reaches the type's own dispatch branch rather than dying at an
    earlier, unrelated required-arg check (Finding 2).

    Returns (returncode, combined stdout+stderr). Uses the same interpreter
    running this test (sys.executable) rather than relying on PATH resolution.
    creationflags is the portable Windows-console-popup guard (resolves to a
    no-op 0 on non-Windows platforms).
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
        *_required_args_for(doc_type),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def test_no_unreachable_doc_type_across_known_types() -> None:
    """For every manifest-derived known --type, the scaffold must never fall
    through to the `raise AssertionError(f"unreachable doc_type: ...")`
    backstop in coordinator-doc-new's main() dispatch chain.

    A type reaching its own required-arg validation error (or scaffolding
    cleanly) both count as PASS — this test isolates the missing-emitter-
    branch defect class only, not full per-type argument coverage.
    """
    known_types = sorted(_known_types())
    if not known_types:
        raise AssertionError(
            "coordinator_registry.KNOWN_TYPES resolved empty — manifest "
            "load likely broken; this test cannot proceed without a real "
            "known-types set to iterate."
        )

    with tempfile.TemporaryDirectory(prefix="coordinator-doc-new-emitter-parity-") as tmpdir:
        for doc_type in known_types:
            name = f"--type {doc_type}: does not emit '{_UNREACHABLE_MARKER}'"

            if doc_type in _EXPLICIT_SKIPS:
                print(f"  SKIP: {name} — {_EXPLICIT_SKIPS[doc_type]}")
                continue

            out_path = os.path.join(tmpdir, f"{doc_type}.md")
            try:
                _rc, combined_output = _invoke_scaffold(doc_type, out_path)
            except subprocess.TimeoutExpired:
                raise AssertionError(f"{name}: " + ("scaffold invocation timed out after 60s"))

            if _UNREACHABLE_MARKER in combined_output:
                raise AssertionError(f"{name}: " + (f"scaffold emitted '{_UNREACHABLE_MARKER}' — manifest-registered "
                    f"type '{doc_type}' has no emitter branch in coordinator-doc-new's "
                    f"main() dispatch chain. Output: {combined_output.strip()[:500]}"))


def test_review_findings_divergence_parses_as_object() -> None:
    """review-findings' scaffolded `divergence` field must parse as an object
    under this repo's restricted YAML parser, not a raw string.

    Regression net for the flow-style defect fixed alongside this test:
    `_scaffold_review_findings` (coordinator-doc-new) previously emitted
    `divergence: {diverged: false}` (flow-style mapping) instead of block
    style. coordinator_core.frontmatter.schema_validate.parse_yaml does not
    support flow-style mappings (documented restriction — see that module's
    docstring and _scaffold_run_report's docstring in coordinator-doc-new) and
    parses a flow-style value as a raw string, tripping the run-report
    schema's object-shaped `divergence` check
    (type object, required [diverged]) on every scaffolded review-findings
    sidecar. Block style (key on its own line, nested `diverged:` indented
    below) is the only shape that round-trips through parse_yaml as a dict.
    """
    repo_root = os.path.dirname(os.path.dirname(_repo_bin_dir()))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from coordinator_core.frontmatter.schema_validate import parse_yaml

    name = "--type review-findings: divergence parses as object, not string"
    with tempfile.TemporaryDirectory(prefix="coordinator-doc-new-divergence-shape-") as tmpdir:
        out_path = os.path.join(tmpdir, "review-findings.md")
        rc, combined_output = _invoke_scaffold("review-findings", out_path)
        if rc != 0:
            raise AssertionError(f"{name}: " + (f"scaffold invocation failed (rc={rc}): {combined_output.strip()[:500]}"))

        with open(out_path, encoding="utf-8") as f:
            content = f.read()

        frontmatter = content.split("---", 2)[1]
        parsed = parse_yaml(frontmatter)
        divergence = parsed.get("divergence")
        if not isinstance(divergence, dict):
            raise AssertionError(f"{name}: " + (f"divergence parsed as {type(divergence).__name__} ({divergence!r}), "
                "expected a dict — flow-style regression"))
        if "diverged" not in divergence:
            raise AssertionError(f"{name}: " + (f"divergence dict missing required 'diverged' key: {divergence!r}"))

