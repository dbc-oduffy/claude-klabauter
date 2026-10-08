"""Structural enforcement for DR-445 (assemble-in-a-throwaway, move once).

docs/decisions/DR-445-publish-assembles-in-a-throwaway-and-moves-once.md § Enforcement:
"A test pins the order structurally: no destination write precedes the last
gate, and no gate follows the first destination write."

Pinned names this test is written against (two peers land these concurrently
— this file is RED until both do, by design):
  - coordinator/bin/publish.py: `_run_round_dr445`, `_swap_all_rows_into_dest`,
    `process_target` (stage-only post-DR445).
  - coordinator/lib/percolate/throwaway_tree.py: `build_throwaway_tree`,
    `discard_throwaway_tree`.

Run: python -m pytest coordinator/bin/tests/test_publish_order_dr445.py -q
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Gate/round-boundary names DR-445 requires to bracket the throwaway build/
# swap/commit/discard sequence. Every one of these must exist as a module
# attribute on `publish` post-DR445 — a missing one is a red test, not a
# skip, because the spec pins these exact names.
_GATE_NAMES = (
    "run_pre_sync_gates",
    "dispatch_percolate_post_rsync",
    "dispatch_percolate_pre_ci",
    "dispatch_percolate_inject",
    "dispatch_preswap_function_gate",
    "dispatch_preswap_payload_parity_gate",
    "dispatch_end_of_run_assembled_mirror_gate",
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "publish-order-dr445-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Publish Order DR445 Test")
    _git(root, "config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "chore: init")
    _git(root, "remote", "add", "origin", str(root))
    _git(root, "fetch", "--no-tags", "origin")
    _git(root, "branch", "--set-upstream-to=origin/main", "main")


def _porcelain(root: Path) -> str:
    return _git(root, "status", "--porcelain").stdout


def _mtimes(root: Path) -> dict:
    return {
        str(p): p.stat().st_mtime_ns
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_order_dr445_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _row_string(name: str, src: Path, dst: Path) -> str:
    src.mkdir(parents=True, exist_ok=True)
    return f"{name}|mirror|{src}|{dst}"


def _wire_common_fakes(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter=None, **_: list(rows)
    )

    class _FakeClaudeKlabauter:
        def resolve_target(self, store, name):
            raise KeyError(name)

        def run_parse_sweep(self, repo_root):
            return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

        def enumerate_gate_entrypoints(self, repo_root):
            return ()

    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _FakeClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: tmp_path / "store.yaml")
    monkeypatch.setattr(publish, "resolve_percolate_identity_path", lambda setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_present", lambda path, setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda path: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda path: publish.PercolateIdentity(review=["dummy-pattern"]),
    )
    monkeypatch.setattr(publish, "_resolve_publish_sync_module_path", lambda setup_dir: tmp_path / "publish_sync.py")
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)

    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_function_gate", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_entrypoint_gate", lambda *a, **k: True)


def _wrap_and_record(monkeypatch, name, record, call_log=None):
    """Monkeypatch `publish.<name>` with a wrapper that appends `name` to
    `record` (and, if `call_log` is given, `(name, args, kwargs)`) then
    calls through to the original. Raises AttributeError (an expected red
    failure, per this file's module docstring) if `name` does not yet exist
    on `publish` — i.e. before the peer chunk lands it."""
    original = getattr(publish, name)

    def _wrapper(*args, **kwargs):
        record.append(name)
        if call_log is not None:
            call_log.append((name, args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(publish, name, _wrapper)


def _build_round_fixture(monkeypatch, tmp_path, *, fail_gate: str = None):
    """One-row round with real gates/build/swap/commit/discard wrapped for
    recording. `process_target` is faked (its own staging behavior is
    covered separately by AC3's static check and by the peer chunk's own
    tests) — it only marks the row processed; it must NEVER touch
    `target.dest_dir` directly per DR-445."""
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)
    dest_a = dest_root / "sub-a"
    rows = [_row_string("row-a", tmp_path / "src-a", dest_a)]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    record: list = []
    call_log: list = []

    for gate_name in _GATE_NAMES:
        if fail_gate is not None and gate_name == fail_gate:
            monkeypatch.setattr(publish, gate_name, lambda *a, **k: (record.append(gate_name), False)[1])
        else:
            _wrap_and_record(monkeypatch, gate_name, record, call_log)

    _wrap_and_record(monkeypatch, "build_throwaway_tree", record, call_log)
    _wrap_and_record(monkeypatch, "discard_throwaway_tree", record, call_log)
    _wrap_and_record(monkeypatch, "_swap_all_rows_into_dest", record, call_log)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        record.append("process_target:row-a")
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    return dest_root, rows, record, call_log


def test_event_order_build_gates_swap_commit_discard(monkeypatch, tmp_path):
    dest_root, rows, record, _call_log = _build_round_fixture(monkeypatch, tmp_path)

    rc = publish.main(["row-a"])

    assert rc == 0, f"expected a clean round, got rc={rc}"
    assert "build_throwaway_tree" in record
    assert "_swap_all_rows_into_dest" in record
    assert "discard_throwaway_tree" in record

    build_idx = record.index("build_throwaway_tree")
    swap_idx = record.index("_swap_all_rows_into_dest")
    discard_idx = record.index("discard_throwaway_tree")

    gate_indices = [record.index(g) for g in _GATE_NAMES if g in record]
    assert gate_indices, "no gate was recorded — wrappers did not fire"

    assert build_idx < min(gate_indices), (
        "build_throwaway_tree must precede every gate", record
    )
    assert max(gate_indices) < swap_idx, (
        "every gate must precede the swap", record
    )
    # The round commit (2026-09-29 PM ruling: commit-in-throwaway then
    # fetch + `merge --ff-only`) now runs INSIDE `_swap_all_rows_into_dest`
    # itself rather than as a separate recorded event straight after it —
    # "nothing runs between the swap and the commit" now reads as "the swap
    # IS the commit", so the only remaining ordering obligation is that
    # nothing else this test wraps fires between the swap and the discard.
    assert swap_idx < discard_idx, (
        "the swap (which now commits) must precede the discard", record
    )
    assert record[swap_idx + 1] == "discard_throwaway_tree", (
        "no other recorded event may run between the swap and the discard",
        record,
    )
    assert discard_idx == len(record) - 1, (
        "discard_throwaway_tree must be the last recorded event", record
    )


def test_failing_gate_leaves_dest_untouched_and_discards_throwaway(monkeypatch, tmp_path):
    dest_root, rows, record, _call_log = _build_round_fixture(
        monkeypatch, tmp_path, fail_gate="dispatch_end_of_run_assembled_mirror_gate"
    )
    before_mtimes = _mtimes(dest_root)
    before_porcelain = _porcelain(dest_root)

    rc = publish.main(["row-a"])

    assert rc != 0, "a failing gate must not report success"
    assert "_swap_all_rows_into_dest" not in record, (
        "the swap must never fire when a gate fails", record
    )
    assert "discard_throwaway_tree" in record, (
        "the throwaway must still be discarded on a failing round", record
    )
    assert record[-1] == "discard_throwaway_tree", (
        "discard is the last event pass or fail", record
    )

    after_porcelain = _porcelain(dest_root)
    after_mtimes = _mtimes(dest_root)
    assert after_porcelain == before_porcelain == "", (
        f"dest must stay clean on a failing gate, got: {after_porcelain!r}"
    )
    assert after_mtimes == before_mtimes, (
        "no file under dest_dir may be touched (written or re-stat'd) when "
        "the round fails before the swap"
    )


# Every end-of-run gate `_run_round_dr445` dispatches (§ its own Phase 3
# comment) — distinct from `_GATE_NAMES` above, which mixes in per-row
# pre-sync/preswap gate names used only for the build/gate/swap/commit/
# discard ordering assertions. The peer chunk retargets ALL of these to the
# throwaway root, so this list must name every one of them, not just
# `dispatch_end_of_run_assembled_mirror_gate`.
_END_OF_RUN_GATE_NAMES = (
    "dispatch_end_of_run_identity_check",
    "dispatch_end_of_run_install_doc_payload_check",
    "dispatch_end_of_run_unscanned_published_check",
    "dispatch_end_of_run_function_gate",
    "dispatch_end_of_run_entrypoint_gate",
    "dispatch_end_of_run_argv_parity_gate",
    "dispatch_end_of_run_assembled_mirror_gate",
)


def _root_args(value):
    """Flatten a gate call's positional/keyword value into the Path-like
    root args it carries — a bare `Path`/str, every element of a
    `List[Path]`/tuple/set, or every KEY of a `dict[Path, ...]` (several
    end-of-run gates take `*_by_repo_root` dicts keyed by root rather than a
    `repo_roots` list — § `dispatch_end_of_run_unscanned_published_check`).
    Non-path, non-container values yield nothing."""
    if isinstance(value, dict):
        for key in value:
            yield from _root_args(key)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _root_args(item)
    elif isinstance(value, (str, Path)):
        yield value


def test_gates_receive_the_throwaway_root_never_the_dest(monkeypatch, tmp_path):
    """Every end-of-run gate call this round makes must be handed the
    throwaway tree's root — as a scalar OR inside a `repo_roots: List[Path]`
    — never `target.dest_dir`. Checked across ALL end-of-run gates
    (`_END_OF_RUN_GATE_NAMES`), not just the assembled-mirror gate: the peer
    is retargeting every one of them to the throwaway."""
    dest_root, rows, record, call_log = _build_round_fixture(monkeypatch, tmp_path)

    # `_wire_common_fakes` stubbed most end-of-run gates to `lambda *a, **k:
    # True` (no recording) so the build/gate/swap/commit ordering tests above
    # stay narrow. Re-wrap every one of them here so this test's `call_log`
    # actually captures their root args.
    for gate_name in _END_OF_RUN_GATE_NAMES:
        _wrap_and_record(monkeypatch, gate_name, record, call_log)

    sentinel_throwaway = tmp_path / "throwaway-marker"
    # A real (if trivial) git repo: post-DR445 the swap itself reads `git
    # status` INSIDE the throwaway (§ `_throwaway_delta_paths`) rather than
    # only gates reading it, so a bare directory here now fails the swap
    # with "not a git repository" before this test ever gets to check gate
    # root args. An empty repo's status is clean, so the swap sees no delta
    # and skips straight past — this test is scoped to gate args, not swap
    # content.
    _init_git_repo(sentinel_throwaway)

    def _fake_build(*args, **kwargs):
        record.append("build_throwaway_tree")
        return sentinel_throwaway

    monkeypatch.setattr(publish, "build_throwaway_tree", _fake_build)

    rc = publish.main(["row-a"])
    assert rc == 0, f"expected a clean round, got rc={rc}"

    dest_a = dest_root / "sub-a"
    seen_gate_names = set()
    for name, args, kwargs in call_log:
        if name not in _END_OF_RUN_GATE_NAMES:
            continue
        seen_gate_names.add(name)
        # `percolate_root`/`out` are real, non-root kwargs some gates also
        # take (§ `dispatch_end_of_run_identity_check`'s own signature) —
        # deliberately excluded here since neither is a "root argument" this
        # test is scoped to; everything else (positional AND the remaining
        # kwargs, including every `*_by_repo_root` dict) is in scope.
        _non_root_kwarg_names = {"percolate_root", "out"}
        values_in_scope = list(args) + [
            v for k, v in kwargs.items() if k not in _non_root_kwarg_names
        ]
        root_values = []
        for value in values_in_scope:
            root_values.extend(_root_args(value))
        assert str(dest_a) not in [str(v) for v in root_values], (
            f"{name} received target.dest_dir among its root args, never allowed post-DR-445",
            args,
            kwargs,
        )
        assert root_values, (f"{name} carried no Path-like root args to check", args, kwargs)
        assert all(str(sentinel_throwaway) == str(v) for v in root_values), (
            f"{name} was handed a root that is not the throwaway tree", args, kwargs, root_values
        )

    assert seen_gate_names == set(_END_OF_RUN_GATE_NAMES), (
        "not every end-of-run gate fired this round", seen_gate_names
    )


def test_process_target_has_no_static_write_into_dest_dir():
    """AC3 (DR-445 § Enforcement, `process_target` becomes stage-only): the
    AST of `process_target` must contain no call to the swap function and no
    shutil/os rename-or-copy call whose target names `dest_dir`."""
    source = (_BIN_DIR / "publish.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_BIN_DIR / "publish.py"))

    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "process_target":
            func_node = node
            break
    assert func_node is not None, "process_target not found in publish.py"

    _FORBIDDEN_CALL_NAMES = {
        "_swap_all_rows_into_dest",
        "_swap_publish_staging_into_dest",
        "_swap_publish_staging_into_dest_root",
    }
    _FORBIDDEN_MODULE_FUNCS = {
        ("shutil", "move"),
        ("shutil", "copy"),
        ("shutil", "copy2"),
        ("shutil", "copytree"),
        ("shutil", "rmtree"),
        ("os", "rename"),
        ("os", "replace"),
    }

    violations = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in _FORBIDDEN_CALL_NAMES:
            violations.append(fn.id)
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            pair = (fn.value.id, fn.attr)
            if pair in _FORBIDDEN_MODULE_FUNCS:
                # only a violation if it looks like it targets dest_dir --
                # scan the call's arg source text for "dest_dir"
                call_src = ast.get_source_segment(source, node) or ""
                if "dest_dir" in call_src:
                    violations.append(f"{pair[0]}.{pair[1]}")

    assert not violations, (
        f"process_target contains a write into dest_dir, not allowed post-DR-445: {violations}"
    )
