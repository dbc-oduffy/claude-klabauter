"""
coordinator_core.ops.test_run_pre_ci_hooks

Characterization tests for the "percolate.run_pre_ci_hooks" op
(coordinator_core.ops.run_pre_ci_hooks) — the Python-only pre-CI hook sweep
settled by Wave-3 settlement B3.

Coverage:
  (a) registered under exactly "percolate.run_pre_ci_hooks" on import
  (b) missing hooks_root / dest params each raise a descriptive ValueError
      naming the missing param
  (c) missing pre-ci dir → clean skip (fence nullglob parity)
  (d) empty pre-ci dir → clean skip
  (e) happy path: hooks run in lexical order, each invoked as
      [sys.executable, hook, dest] with dest as argv[1]
  (f) non-zero exit aborts: aborted_at names the failing hook, exit_code
      carries its return code, later hooks never run
  (g) .sh file encountered → ValueError naming the file and the migration
      requirement, raised BEFORE any hook runs (named acceptance criterion —
      settlement B3 fail-loud, no bash fallback, no silent skip)
  (h) idempotency (AC7/CC-4): double invocation with identical inputs and an
      idempotent hook returns an identical result (per-hook idempotency is
      caller-hook-dependent per the DEC-7 note; proven here with a hook that
      is itself idempotent)

Spec backlink: pln-wave-3-design-settlements-15-d-76fdbd § B3
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.run_pre_ci_hooks  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.run_pre_ci_hooks import _run_pre_ci_hooks

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "percolate.run_pre_ci_hooks"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.run_pre_ci_hooks @register_op did not fire"
)


def _make_hooks_tree(tmp_path):
    """Create <hooks_root>/pre-ci and a dest dir; return (hooks_root, pre_ci, dest)."""
    hooks_root = tmp_path / "percolate-hooks" / "some-target"
    pre_ci = hooks_root / "pre-ci"
    pre_ci.mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    return hooks_root, pre_ci, dest


def _write_marker_hook(pre_ci, name, marker_dir, exit_code=0):
    """Write a Python hook that appends '<name>:<argv1>' to marker_dir/order.log.

    The append is to a per-run log so tests can assert execution ORDER and the
    dest argv; exit_code lets a test simulate a failing hook.
    """
    hook = pre_ci / name
    hook.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"log = Path({str(marker_dir)!r}) / 'order.log'\n"
        "with log.open('a', encoding='utf-8') as fh:\n"
        f"    fh.write({name!r} + ':' + sys.argv[1] + '\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return hook


def _read_order_log(marker_dir):
    log = marker_dir / "order.log"
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def test_op_registered():
    assert _OP_NAME in _REGISTRY


def test_missing_hooks_root_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="hooks_root"):
        _run_pre_ci_hooks({"dest": str(tmp_path)})


def test_missing_dest_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="dest"):
        _run_pre_ci_hooks({"hooks_root": str(tmp_path)})


def test_missing_pre_ci_dir_is_clean_skip(tmp_path):
    hooks_root = tmp_path / "percolate-hooks" / "some-target"
    hooks_root.mkdir(parents=True)  # no pre-ci subdir
    result = _run_pre_ci_hooks(
        {"hooks_root": str(hooks_root), "dest": str(tmp_path)}
    )
    assert result == {"hooks_run": [], "aborted_at": None, "exit_code": 0}


def test_empty_pre_ci_dir_is_clean_skip(tmp_path):
    hooks_root, _pre_ci, dest = _make_hooks_tree(tmp_path)
    result = _run_pre_ci_hooks(
        {"hooks_root": str(hooks_root), "dest": str(dest)}
    )
    assert result == {"hooks_run": [], "aborted_at": None, "exit_code": 0}


def test_hooks_run_in_lexical_order_with_dest_argv(tmp_path):
    hooks_root, pre_ci, dest = _make_hooks_tree(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    # Written out of lexical order deliberately.
    _write_marker_hook(pre_ci, "20-second.py", marker_dir)
    _write_marker_hook(pre_ci, "10-first.py", marker_dir)

    result = _run_pre_ci_hooks(
        {"hooks_root": str(hooks_root), "dest": str(dest)}
    )

    assert result == {
        "hooks_run": ["10-first.py", "20-second.py"],
        "aborted_at": None,
        "exit_code": 0,
    }
    assert _read_order_log(marker_dir) == [
        f"10-first.py:{dest}",
        f"20-second.py:{dest}",
    ]


def test_nonzero_exit_aborts_and_skips_later_hooks(tmp_path):
    hooks_root, pre_ci, dest = _make_hooks_tree(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    _write_marker_hook(pre_ci, "10-ok.py", marker_dir)
    _write_marker_hook(pre_ci, "20-fails.py", marker_dir, exit_code=3)
    _write_marker_hook(pre_ci, "30-never.py", marker_dir)

    result = _run_pre_ci_hooks(
        {"hooks_root": str(hooks_root), "dest": str(dest)}
    )

    assert result == {
        "hooks_run": ["10-ok.py"],
        "aborted_at": "20-fails.py",
        "exit_code": 3,
    }
    # The failing hook DID run (its marker is present); the later one did not.
    lines = _read_order_log(marker_dir)
    assert f"20-fails.py:{dest}" in lines
    assert not any(line.startswith("30-never.py:") for line in lines)


def test_sh_hook_encountered_raises_naming_the_file(tmp_path):
    """Named acceptance criterion (settlement B3): a .sh file in the pre-ci
    dir is a structured error naming it — no bash fallback, no silent skip."""
    hooks_root, pre_ci, dest = _make_hooks_tree(tmp_path)
    (pre_ci / "10-legacy.sh").write_text("exit 0\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        _run_pre_ci_hooks({"hooks_root": str(hooks_root), "dest": str(dest)})

    msg = str(excinfo.value)
    assert "10-legacy.sh" in msg
    assert "Python-only" in msg


def test_sh_hook_blocks_before_any_py_hook_runs(tmp_path):
    """A mixed .py/.sh dir runs NOTHING — the .sh check precedes execution."""
    hooks_root, pre_ci, dest = _make_hooks_tree(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    _write_marker_hook(pre_ci, "10-ok.py", marker_dir)
    (pre_ci / "20-legacy.sh").write_text("exit 0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="20-legacy.sh"):
        _run_pre_ci_hooks({"hooks_root": str(hooks_root), "dest": str(dest)})

    assert _read_order_log(marker_dir) == []


def test_double_invocation_identical_result(tmp_path):
    """AC7/CC-4 idempotency proof: with an idempotent hook, the second call
    with identical inputs is a safe no-op returning the documented shape.
    Per-hook idempotency is caller-hook-dependent (DEC-7 note in the module
    docstring); this test supplies a hook that is itself idempotent."""
    hooks_root, pre_ci, dest = _make_hooks_tree(tmp_path)
    hook = pre_ci / "10-idempotent.py"
    # Idempotent by construction: overwrite (not append) a sentinel file.
    hook.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1], 'sentinel.txt').write_text('ok', encoding='utf-8')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    params = {"hooks_root": str(hooks_root), "dest": str(dest)}

    first = _run_pre_ci_hooks(params)
    second = _run_pre_ci_hooks(params)

    assert first == second == {
        "hooks_run": ["10-idempotent.py"],
        "aborted_at": None,
        "exit_code": 0,
    }
    assert (dest / "sentinel.txt").read_text(encoding="utf-8") == "ok"
