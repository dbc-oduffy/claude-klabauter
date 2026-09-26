"""
coordinator_core.ops.tests.test_cutover_gate_derivation

Standalone unit tests for coordinator_core.ops.cutover_gate.derive — the pure
value-vocabulary derivation function (no op registration; this module tests
the C4a deliverable in isolation from the later C4b op-handler wave).

Coverage:
    (a) writer-context string literal (assignment) is detected
    (b) reader-context string literal (comparison) is detected
    (c) both modes run unconditionally on a file containing both contexts —
        proves the derivation is not writer-only (the DR-084 near-miss shape)
    (d) a non-Python (.js) hardcoded string-literal reader is detected via
        the regex pass, exercising the AST-blind-spot case directly
    (e) a foreign repo is reported scanned:false and contributes no ids
    (f) an unmapped (repo_roots-absent) non-foreign repo is also
        scanned:false — fail-closed on absence, not just on declared foreign
    (g) derived_ids/derived_count agree and are sorted + de-duplicated
    (h) an unsupported gate_source.kind raises UnsupportedGateSourceKind
    (i) paths[] containment: a traversal-shaped path entry is silently
        skipped, not read
    (j) FIX-D — an extensionless file with a python3 shebang is derived via
        the AST-first path exactly as a .py file is (the named house trap:
        "count by shebang, not by extension")
    (k) FIX-D — an extensionless file with a non-python shebang (#!/bin/bash)
        is NOT derived (not misclassified as Python, not swept in by the
        text-suffix regex pass either)
    (l) FIX-D — an extensionless, undecodable (binary) file is skipped
        without raising
    (m) precision fix — a Python module DOCSTRING mention is NOT derived
        (usage-context classification, not a structural position)
    (n) precision fix — a Python COMMENT mention is NOT derived (comments
        are never AST nodes, so this also proves the pattern isn't being
        grepped over raw source as a fallback)
    (o) precision fix — a markdown BODY PROSE mention is NOT derived, but a
        markdown FRONTMATTER field value IS
    (p) precision fix — a real structural Python usage (comparison operand)
        IS derived, proving the tightened classifier still catches genuine
        readers/writers and isn't vacuous

Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md § C4a, FIX-D,
FIX-F (derivation precision)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.cutover_gate import UnsupportedGateSourceKind, _is_prose, derive


@pytest.fixture()
def repo_tree(tmp_path: Path) -> Path:
    root = tmp_path / "doe"
    (root / "coordinator" / "lib").mkdir(parents=True)
    (root / "coordinator" / "web").mkdir(parents=True)
    return root


def test_writer_context_detected(repo_tree: Path) -> None:
    writer_file = repo_tree / "coordinator" / "lib" / "writer.py"
    writer_file.write_text("deployment_state = 'closed'\n", encoding="utf-8")

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/lib/writer.py"]
    assert result["derived_count"] == 1


def test_reader_context_detected(repo_tree: Path) -> None:
    reader_file = repo_tree / "coordinator" / "lib" / "reader.py"
    reader_file.write_text(
        "def check(state):\n    return state == 'closed'\n", encoding="utf-8"
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/lib/reader.py"]


def test_both_modes_run_unconditionally(repo_tree: Path) -> None:
    both_file = repo_tree / "coordinator" / "lib" / "both.py"
    both_file.write_text(
        "STATE = 'closed'\n\n\ndef is_closed(s):\n    return s == 'closed'\n",
        encoding="utf-8",
    )
    reader_only = repo_tree / "coordinator" / "lib" / "reader_only.py"
    reader_only.write_text("assert x == 'closed'\n", encoding="utf-8")

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert set(result["derived_ids"]) == {
        "doe:coordinator/lib/both.py",
        "doe:coordinator/lib/reader_only.py",
    }


def test_js_hardcoded_reader_detected_via_regex(repo_tree: Path) -> None:
    js_file = repo_tree / "coordinator" / "web" / "consumed-marker.js"
    js_file.write_text(
        "const TERMINAL_DEPLOYMENT = ['closed', 'archived'];\n", encoding="utf-8"
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/web"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/web/consumed-marker.js"]


def test_foreign_repo_not_scanned_and_contributes_nothing(repo_tree: Path) -> None:
    writer_file = repo_tree / "coordinator" / "lib" / "writer.py"
    writer_file.write_text("x = 'closed'\n", encoding="utf-8")

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [
            {"repo": "doe", "foreign": False},
            {"repo": "cockpit", "foreign": True},
        ],
    }
    result = derive(gate_source, {"doe": repo_tree})

    repos_by_name = {entry["repo"]: entry for entry in result["repos"]}
    assert repos_by_name["cockpit"] == {
        "repo": "cockpit",
        "foreign": True,
        "scanned": False,
    }
    assert repos_by_name["doe"]["scanned"] is True
    assert all(not rid.startswith("cockpit:") for rid in result["derived_ids"])


def test_unmapped_non_foreign_repo_is_also_unscanned(repo_tree: Path) -> None:
    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "claude-klabauter", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["repos"] == [{"repo": "claude-klabauter", "foreign": False, "scanned": False}]
    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_unsupported_kind_raises() -> None:
    gate_source = {"kind": "shell-sweep", "pattern": "closed", "paths": [], "repos": []}
    with pytest.raises(UnsupportedGateSourceKind):
        derive(gate_source, {})


def test_paths_traversal_entry_is_skipped_not_read(repo_tree: Path) -> None:
    outside = repo_tree.parent / "outside.py"
    outside.write_text("x = 'closed'\n", encoding="utf-8")

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["../outside.py"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_symlinked_subdir_escaping_root_is_not_walked(repo_tree: Path) -> None:
    outside_dir = repo_tree.parent / "outside_pkg"
    outside_dir.mkdir()
    (outside_dir / "leaked.py").write_text("x = 'closed'\n", encoding="utf-8")

    scanned_dir = repo_tree / "coordinator" / "lib"
    try:
        (scanned_dir / "escape_link").symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not supported on this platform/permissions")

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_extensionless_python_shebang_file_is_derived(repo_tree: Path) -> None:
    shebang_file = repo_tree / "coordinator" / "lib" / "some-cli"
    shebang_file.write_text(
        "#!/usr/bin/env python3\nx = 'closed'\n", encoding="utf-8"
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/lib/some-cli"]


def test_extensionless_non_python_shebang_file_is_not_derived(repo_tree: Path) -> None:
    shebang_file = repo_tree / "coordinator" / "lib" / "some-script"
    shebang_file.write_text(
        "#!/bin/bash\necho closed\n", encoding="utf-8"
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_is_prose_one_word_trailing_punctuation_boundary() -> None:
    """[nit] the ``_MAX_STRUCTURAL_VALUE_WORDS``
    one-word-or-less threshold has no test exercising a value that is
    whitespace-wise one "word" (so classified structural/non-prose) but is
    actually still prose because of trailing punctuation, e.g. a YAML
    ``key: value`` line whose value is a single word ending in a period.
    Not asserting a specific "should be prose" outcome (the reviewer could
    not construct a real false positive and this test does not restructure
    `_is_prose`) — this pins the CURRENT documented behavior of that
    boundary so a future change to the heuristic is a visible, deliberate
    diff rather than a silent regression."""
    assert _is_prose("notes: closed.") is False
    assert _is_prose("notes: this was closed after review.") is True


def test_docstring_mention_is_not_derived(repo_tree: Path) -> None:
    docstring_only = repo_tree / "coordinator" / "lib" / "docstring_only.py"
    docstring_only.write_text(
        '"""This module fails closed when the handoff was closed by a peer."""\n'
        "\n"
        "def noop():\n"
        "    pass\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": r"\bclosed\b",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_comment_mention_is_not_derived(repo_tree: Path) -> None:
    comment_only = repo_tree / "coordinator" / "lib" / "comment_only.py"
    comment_only.write_text(
        "# the ticket was closed after review\n"
        "def noop():\n"
        "    pass\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": r"\bclosed\b",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_markdown_body_prose_not_derived_but_frontmatter_value_is(repo_tree: Path) -> None:
    web_dir = repo_tree / "coordinator" / "web"
    prose_md = web_dir / "prose-only.md"
    prose_md.write_text(
        "# Notes\n\nThe migration continued after the outage was resolved.\n",
        encoding="utf-8",
    )
    frontmatter_md = web_dir / "frontmatter-value.md"
    frontmatter_md.write_text(
        "---\ndeployment_state: closed\n---\n\n# Record\n\nSome unrelated body text.\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": r"\bclosed\b|\bcontinued\b",
        "paths": ["coordinator/web"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/web/frontmatter-value.md"]
    assert result["derived_count"] == 1


def test_real_structural_usage_is_still_derived(repo_tree: Path) -> None:
    structural_file = repo_tree / "coordinator" / "lib" / "flag_check.py"
    structural_file.write_text(
        '"""This module handles a flag that was continued from a prior release."""\n'
        "\n"
        "# continued below\n"
        "def check(args):\n"
        "    if '--continued-into' in args:\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": r"\bcontinued\b",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/lib/flag_check.py"]
    assert result["derived_count"] == 1


def test_markdown_fenced_command_call_site_with_args_is_derived(repo_tree: Path) -> None:
    """Regression — census fold-in constraint 1 (state/memos/2026-07-25-
    census-requirement-folded-into-cutover-primitive.md § "The requirement
    handed forward", item 1): a command invocation inside a fenced code
    block — the exact ``resolve-python.sh`` shape (456 call sites hidden
    inside ``.md`` command fences, per that memo and ``CLAUDE.local.md``) —
    carries the command name PLUS its flags/arguments, so the fence LINE is
    multiple whitespace-separated tokens. ``_is_prose``'s word-count
    heuristic (``_MAX_STRUCTURAL_VALUE_WORDS = 1``) was written for
    frontmatter/body VALUE description strings and, applied unmodified to
    fence content, excludes a genuine multi-token call site as if it were a
    human sentence — reproducing the resolve-python.sh blind spot inside the
    very primitive built to catch it, in a different guise (a real call site
    silently invisible to the derivation rather than an extension-filtered
    file). Fence content is CODE by definition (delimited by ``` ```` ```` ```
    fences), never body prose — the prose filter must not apply to it."""
    fence_md = repo_tree / "coordinator" / "web" / "usage.md"
    fence_md.write_text(
        "# Usage\n\n```bash\narchive-stamp-cli --close /some/path\n```\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "archive-stamp-cli",
        "paths": ["coordinator/web"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/web/usage.md"]
    assert result["derived_count"] == 1


def test_markdown_fenced_comment_line_is_not_derived(repo_tree: Path) -> None:
    fence_md = repo_tree / "coordinator" / "web" / "usage.md"
    fence_md.write_text(
        "# Usage\n\n```bash\n# this record was closed after review\n```\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": r"\bclosed\b",
        "paths": ["coordinator/web"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_markdown_fenced_prose_sentence_is_accepted_tradeoff_false_positive(repo_tree: Path) -> None:
    """Pins the ACCEPTED tradeoff named in Finding 2 (code-reviewer): a
    non-comment prose sentence inside a fence (a pasted log/transcript
    line, a free-text description in an example value) that happens to
    whole-token-match the pattern is NOT filtered — there is no reliable
    structural signal distinguishing this shape from a genuine multi-token
    call site without reintroducing the word-count heuristic
    (``_is_prose``) the positive-case fix deliberately removed from fence
    lines. This test documents the current (accepted) false-positive
    behavior rather than asserting it away — narrowing this further is out
    of scope per the F2 disposition (see cutover_gate.py
    ``_scan_markdown_file`` docstring negative-spec)."""
    fence_md = repo_tree / "coordinator" / "web" / "transcript.md"
    fence_md.write_text(
        "# Transcript\n\n```text\nthis record was closed after review\n```\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": r"\bclosed\b",
        "paths": ["coordinator/web"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/web/transcript.md"]
    assert result["derived_count"] == 1


def test_markdown_fenced_non_call_site_code_reference_is_accepted_tradeoff(repo_tree: Path) -> None:
    fence_md = repo_tree / "coordinator" / "web" / "example.md"
    fence_md.write_text(
        '# Example\n\n```python\nexample_command = "archive-stamp-cli"\n```\n',
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "archive-stamp-cli",
        "paths": ["coordinator/web"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == ["doe:coordinator/web/example.md"]
    assert result["derived_count"] == 1


def test_extensionless_binary_file_is_skipped_without_raising(repo_tree: Path) -> None:
    binary_file = repo_tree / "coordinator" / "lib" / "some-binary"
    binary_file.write_bytes(b"\xff\xfe\x00\x01closed\x00\x02")

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0


def test_unparseable_py_file_matching_pattern_is_unknown_not_dead(repo_tree: Path) -> None:
    broken_file = repo_tree / "coordinator" / "lib" / "broken.py"
    broken_file.write_text(
        "def broken(:\n    TOKEN = 'closed'\n",
        encoding="utf-8",
    )

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["derived_ids"] == []
    assert result["derived_count"] == 0
    assert result["unknown_ids"] == ["doe:coordinator/lib/broken.py"]
    assert result["unknown_count"] == 1


def test_unparseable_py_file_not_matching_pattern_is_not_unknown(repo_tree: Path) -> None:
    broken_file = repo_tree / "coordinator" / "lib" / "broken.py"
    broken_file.write_text("def broken(:\n    TOKEN = 'irrelevant'\n", encoding="utf-8")

    gate_source = {
        "kind": "value-vocabulary",
        "pattern": "closed",
        "paths": ["coordinator/lib"],
        "repos": [{"repo": "doe", "foreign": False}],
    }
    result = derive(gate_source, {"doe": repo_tree})

    assert result["unknown_ids"] == []
    assert result["unknown_count"] == 0
