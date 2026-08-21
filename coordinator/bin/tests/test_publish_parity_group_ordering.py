"""coordinator/bin/tests/test_publish_parity_group_ordering.py — regression
test for the klabauter parity-group publish ordering (2026-08-15,
docs/plans/2026-08-15-bind-the-klabauter-publish-rows-into-a-parity-group.md,
chunk C3).

`claude-klabauter` (source `coordinator_core`) COMPOSES argv and hands it to
five entrypoint-carrying klabauter rows, which PARSE it with strict
`argparse.parse_args`. Each row in `setup/publish-targets.portable` stages
then swaps independently, so a publish round is internally inconsistent for
its whole duration — the 2026-08-15 live incident (`wsc-tail.py` rejecting a
flag `directives_commit_tail.py` had already started emitting) happened
because the emitter row landed BEFORE its parser rows. The fix orders every
entrypoint row ahead of the emitter row in the table, and this test pins
that ordering two ways: against the real table (parsed through
`load_targets`, so an accidental table edit is caught), and against a
synthetic inverted table that `publish.py::main()`'s own pre-flight must
refuse rather than silently publish.

Run: python -m pytest coordinator/bin/tests/test_publish_parity_group_ordering.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent
_LIB_DIR = _REPO_ROOT / "coordinator" / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from percolate.targets import load_targets  # noqa: E402


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_parity_group_ordering_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_real_table_orders_every_entrypoint_row_before_the_emitter_row():
    """`setup/publish-targets.portable`, parsed through the real `load_targets`
    seam, must place every `_KLABAUTER_PARITY_ENTRYPOINT_ROWS` name before
    `_KLABAUTER_PARITY_EMITTER_ROW`. This is the REASON C3 exists: the
    2026-08-15 incident happened because the emitter (`claude-klabauter`)
    was the FIRST row in the table, publishing ahead of every row that
    parses the argv it composes. A future row insertion that puts the
    emitter back ahead of a parser row must fail this assertion.
    """
    rows = load_targets(_REPO_ROOT / "setup")
    index_by_name = {row.split("|", 1)[0]: i for i, row in enumerate(rows)}

    assert publish._KLABAUTER_PARITY_EMITTER_ROW in index_by_name
    emitter_index = index_by_name[publish._KLABAUTER_PARITY_EMITTER_ROW]

    for entrypoint_name in publish._KLABAUTER_PARITY_ENTRYPOINT_ROWS:
        assert entrypoint_name in index_by_name, (
            f"{entrypoint_name!r} missing from the resolved klabauter rows — "
            "the parity group's own membership has drifted from "
            "`_KLABAUTER_PARITY_ENTRYPOINT_ROWS`."
        )
        assert index_by_name[entrypoint_name] < emitter_index, (
            f"{entrypoint_name!r} (argv parser) must publish BEFORE "
            f"{publish._KLABAUTER_PARITY_EMITTER_ROW!r} (argv emitter): a "
            "parser landing after its emitter can be handed a flag it has "
            "not learned yet — exactly the 2026-08-15 incident this "
            "ordering exists to prevent."
        )


def test_synthetic_additive_ordering_passes_the_assert():
    """Sanity check on the assert's own logic, independent of the real
    table: entrypoints-then-emitter (the shape C3 lands) must pass."""
    synthetic_rows = [
        f"{name}|mirror|publish-mirror:claude_klabauter|src|dst"
        for name in (*publish._KLABAUTER_PARITY_ENTRYPOINT_ROWS, publish._KLABAUTER_PARITY_EMITTER_ROW)
    ]
    assert publish._assert_klabauter_parity_group_ordering(synthetic_rows) is True


def test_synthetic_inverted_table_fails_the_assert_with_the_reason():
    """Emitter-first (the 2026-08-15 incident's own shape) must fail the
    assert, and the failure must name WHY, not just that it failed — a
    future reader deleting this check has to argue with the rationale."""
    synthetic_rows = [
        f"{name}|mirror|publish-mirror:claude_klabauter|src|dst"
        for name in (publish._KLABAUTER_PARITY_EMITTER_ROW, *publish._KLABAUTER_PARITY_ENTRYPOINT_ROWS)
    ]
    assert publish._assert_klabauter_parity_group_ordering(synthetic_rows) is False


def test_synthetic_partial_target_subset_is_not_a_violation():
    """A `--target` subset that resolves the emitter row but none of the
    entrypoint rows is not a parity violation — the check only orders rows
    that are actually PRESENT in this run, never a completeness demand on
    the parity group's membership."""
    synthetic_rows = [
        f"{publish._KLABAUTER_PARITY_EMITTER_ROW}|mirror|publish-mirror:claude_klabauter|src|dst"
    ]
    assert publish._assert_klabauter_parity_group_ordering(synthetic_rows) is True


def test_mains_preflight_refuses_the_synthetic_inverted_table(monkeypatch, tmp_path, capsys):
    """Drive `publish.py::main()` itself (not just the helper in isolation)
    with `load_targets` faked to return the emitter-first synthetic table,
    and assert the run refuses before any row would publish: exit code 1
    (a declined pre-sync gate, per `main`'s own exit-code contract — no row
    has synced yet at this point, so this is NOT the post-publish
    verification exit code 2), and a FATAL naming the ordering reason on
    stderr.
    """
    synthetic_rows = [
        f"{name}|mirror|publish-mirror:claude_klabauter|src|dst"
        for name in (publish._KLABAUTER_PARITY_EMITTER_ROW, *publish._KLABAUTER_PARITY_ENTRYPOINT_ROWS)
    ]

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    monkeypatch.setattr(
        publish,
        "load_targets",
        lambda setup_dir, target_filter=None: synthetic_rows,
    )

    exit_code = publish.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "klabauter parity-group ordering violated" in captured.err
    assert publish._KLABAUTER_PARITY_EMITTER_ROW in captured.err
    for entrypoint_name in publish._KLABAUTER_PARITY_ENTRYPOINT_ROWS:
        assert entrypoint_name in captured.err
