"""
coordinator_core.ops.doc_registry — per-repo human-facing-doc registry reader.

Purpose: doc_staleness.py and doc_content_verify.py (C1/C2/C6b) both need the
same per-repo configuration — which docs count as "human-facing", the
staleness thresholds, and the residual known-good-citation ignore list — read
from `coordinator.local.md` frontmatter, following the existing
`fast_test_cmd`/`full_test_cmd` convention (no new config file). This module
is the single pinned interface both ops (and their tests) author against.

This module also carries `resolve_test_locator_config` (mirrors
`resolve_doc_registry_config`'s resolution pattern), the repo-supplied
test-locator key resolver `coordinator_core.ops.dispatch_emit.pathspec`
reads for its non-`.py` test-target derivation convention. See
`resolve_test_locator_config`'s docstring.

Reuses `coordinator_core.resolve_validation_cmd.cs_read_local_md_key` for the
actual frontmatter extraction — this module does not reimplement YAML
parsing; it only interprets the flat string each key resolves to (a YAML
flow-style list `[a, b, c]` for the two list keys, a bare integer for the two
threshold keys).

Fleet defaults (used whenever a repo declares no override for that key):
    human_facing_docs     = ["README.md", "INSTALL.md", "CONTEXT.md", "CONTRIBUTING.md"]
    doc_staleness_commits  = 8000
    doc_staleness_days     = 21
    doc_verify_ignore      = []

`human_facing_docs` deliberately does NOT default-include `coordinator/README.md`
— that is a DoE-claude-specific plugin-root path (this source tree's one-level
`coordinator/` offset), absent or differently-rooted in every other consumer
repo. Shipping it fleet-wide would emit a permanent no-op `absent` row in
every non-DoE consumer's report. DoE-claude declares it explicitly as its own
per-repo override in its own `coordinator.local.md` frontmatter instead.

A declared doc path that does not exist on disk is NOT this module's concern
— it is returned verbatim as part of the declared list; the consuming op
(doc_staleness.py / doc_content_verify.py) is the layer that classifies a
missing path as `absent`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from coordinator_core.resolve_validation_cmd import cs_read_local_md_key

DEFAULT_HUMAN_FACING_DOCS: tuple[str, ...] = (
    "README.md",
    "INSTALL.md",
    "CONTEXT.md",
    "CONTRIBUTING.md",
)
DEFAULT_DOC_STALENESS_COMMITS = 8000
DEFAULT_DOC_STALENESS_DAYS = 21
DEFAULT_DOC_VERIFY_IGNORE: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocRegistryConfig:

    human_facing_docs: List[str] = field(default_factory=lambda: list(DEFAULT_HUMAN_FACING_DOCS))
    doc_staleness_commits: int = DEFAULT_DOC_STALENESS_COMMITS
    doc_staleness_days: int = DEFAULT_DOC_STALENESS_DAYS
    doc_verify_ignore: List[str] = field(default_factory=lambda: list(DEFAULT_DOC_VERIFY_IGNORE))


def _parse_flow_list(raw: str) -> List[str]:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    items = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
            part = part[1:-1]
        items.append(part)
    return items


def _parse_int(raw: str, default: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_TEST_LOCATOR_SUFFIXES: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestLocatorConfig:

    test_locator_suffixes: List[str] = field(
        default_factory=lambda: list(DEFAULT_TEST_LOCATOR_SUFFIXES)
    )


def resolve_test_locator_config(repo_root: str) -> TestLocatorConfig:
    """Resolve the repo-supplied test-locator config from `coordinator.local.md`.

    Mirrors `resolve_doc_registry_config`'s resolution pattern exactly: one
    `cs_read_local_md_key` call for the single flat top-level key
    `test_locator_suffixes` (a YAML flow-style list, e.g. `[*.test.ts]`),
    parsed via the shared `_parse_flow_list` helper. `repo_root` is the
    consumer repo's root (the directory containing its `coordinator.local.md`),
    never a cwd-implicit default — callers resolve their own root first.

    This key extends `_candidate_test_targets`'s pytest-only (`.py`-stem)
    convention with repo-declared non-`.py` suffix conventions (e.g. a TS
    repo's `*.test.ts` sibling convention). Absent the key (or the file
    itself), resolution degrades to `DEFAULT_TEST_LOCATOR_SUFFIXES` (`[]`) —
    additive, opt-in, byte-identical to today's behaviour when unset, same
    pattern as `execution_mode`'s fails-toward-the-old-default rule. Never
    raises for a missing file, a missing key, or a malformed list value.
    """
    suffixes_raw = cs_read_local_md_key(repo_root, "test_locator_suffixes")
    test_locator_suffixes = (
        _parse_flow_list(suffixes_raw)
        if suffixes_raw.strip()
        else list(DEFAULT_TEST_LOCATOR_SUFFIXES)
    )
    return TestLocatorConfig(test_locator_suffixes=test_locator_suffixes)


def resolve_doc_registry_config(repo_root: str) -> DocRegistryConfig:
    docs_raw = cs_read_local_md_key(repo_root, "human_facing_docs")
    commits_raw = cs_read_local_md_key(repo_root, "doc_staleness_commits")
    days_raw = cs_read_local_md_key(repo_root, "doc_staleness_days")
    ignore_raw = cs_read_local_md_key(repo_root, "doc_verify_ignore")

    human_facing_docs = (
        _parse_flow_list(docs_raw) if docs_raw.strip() else list(DEFAULT_HUMAN_FACING_DOCS)
    )
    doc_verify_ignore = (
        _parse_flow_list(ignore_raw) if ignore_raw.strip() else list(DEFAULT_DOC_VERIFY_IGNORE)
    )

    return DocRegistryConfig(
        human_facing_docs=human_facing_docs,
        doc_staleness_commits=_parse_int(commits_raw, DEFAULT_DOC_STALENESS_COMMITS),
        doc_staleness_days=_parse_int(days_raw, DEFAULT_DOC_STALENESS_DAYS),
        doc_verify_ignore=doc_verify_ignore,
    )
