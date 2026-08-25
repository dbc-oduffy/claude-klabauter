"""Collection shim: makes `guard_message_corpus.py`'s own self-tests actually run.

WHY THIS FILE EXISTS. `guard_message_corpus.py` is the guard-message corpus
SSOT — a data module other test files import rows from — and it also carries
eleven `test_*` functions that check the corpus against live guard behaviour.
Those eleven were never collected by any directory-scoped run: `pyproject.toml`
sets `python_files = ["test_*.py"]`, deliberately, so that the hundreds of
production `.py` scripts living beside tests are never collected. A module
without the `test_` prefix is invisible to that glob no matter what it contains.

So the corpus's own gates were reachable only by typing an explicit node id.
Two of them were failing that way for an unknown period — one on a fixture that
never met its guard's firing condition, one on a genuinely uncovered guard —
and neither could surface in the fast tier. That is the same defect this whole
guard family exists to prevent, wearing test-collection clothes: a check that
does not run, presenting as nothing at all rather than as a failure.

WHY A SHIM AND NOT A RENAME. Renaming `guard_message_corpus.py` to
`test_guard_message_corpus.py` would break every sibling that imports the corpus
rows by module name, and moving the eleven functions out would separate them
from the row definitions they assert against — the coupling is the point. This
file re-exports them by NAME (never `import *`), so the corpus module stays the
single source of truth and pytest collects the functions here.

NEGATIVE SPEC: do not add test logic to this file. It is an import surface. A
new corpus self-test belongs next to the rows in `guard_message_corpus.py`, and
its name added to the import list below — if you forget, the sweep in
`test_every_corpus_self_test_is_collected` fails and tells you.
"""
from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.bash_guards.tests.guard_message_corpus import (  # noqa: F401
    test_ac2_every_reachable_guard_has_a_corpus_row,
    test_advisory_and_platform_expected_speaker_matches_measured_reality,
    test_corpus_imports_cleanly_and_every_row_guard_resolves,
    test_every_advisory_and_platform_guard_has_a_non_firing_control_row,
    test_every_confinement_guard_has_a_non_firing_control_row,
    test_every_hooks_row_guard_has_a_non_firing_control_row,
    test_every_write_guard_has_a_non_firing_control_row,
    test_expected_speaker_matches_measured_reality,
    test_hook_rows_fire_as_expected,
    test_static_text_rows_fire_and_produce_non_empty_text,
    test_write_guard_rows_fire_as_expected,
)

_CORPUS_PATH = Path(__file__).with_name("guard_message_corpus.py")


def test_every_corpus_self_test_is_collected():
    """Every `test_*` in the corpus module must be re-exported above.

    Without this, the shim silently drifts: a self-test added next to the rows
    stays invisible exactly as all eleven were, and the fix would have bought
    one collection pass rather than a durable guarantee. Reads the corpus's
    top-level function names from its AST rather than importing and dir()-ing
    it, so a name is caught even if it is defined but never bound at import.
    """
    tree = ast.parse(_CORPUS_PATH.read_text(encoding="utf-8"), filename=str(_CORPUS_PATH))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }
    missing = sorted(defined - set(globals()))

    assert not missing, (
        "corpus self-test(s) not re-exported by this shim, so no directory-scoped run "
        f"collects them: {missing}. Add each name to the import list at the top of this file."
    )
