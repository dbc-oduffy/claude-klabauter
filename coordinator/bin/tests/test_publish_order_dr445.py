"""Structural enforcement of the publish round's order: stage, gate the union, land.

docs/decisions/DR-445-publish-assembles-in-a-throwaway-and-moves-once.md § Enforcement:
"no destination write precedes the last gate, and no gate follows the first destination
write." The diff-scaled round (`_run_round`) keeps that order without a throwaway clone:
`process_target` stages each row, the end-of-run gates read the per-root staged union, and
`land_diff` is the only destination write.

Run: python -m pytest coordinator/bin/tests/test_publish_order_dr445.py -q
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_END_OF_RUN_GATE_NAMES = (
    "dispatch_end_of_run_identity_check",
    "dispatch_end_of_run_install_doc_payload_check",
    "dispatch_end_of_run_unscanned_published_check",
    "dispatch_end_of_run_function_gate",
    "dispatch_end_of_run_entrypoint_gate",
    "dispatch_end_of_run_argv_parity_gate",
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

        def run_parse_sweep(self, repo_root, **_):
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
    monkeypatch.setattr(publish, "write_publish_provenance_record", lambda **kwargs: None)

    for gate_name in _END_OF_RUN_GATE_NAMES:
        monkeypatch.setattr(publish, gate_name, lambda *a, **k: True)


def _wrap_and_record(monkeypatch, owner, name, record, call_log=None):
    """Replace `owner.<name>` with a wrapper that appends `name` to `record` (and
    `(name, args, kwargs)` to `call_log`) and calls through."""
    original = getattr(owner, name)

    def _wrapper(*args, **kwargs):
        record.append(name)
        if call_log is not None:
            call_log.append((name, args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, _wrapper)


def _stage_payload(dest_dir: Path, files: "dict[str, str]"):
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent)))
    for rel, content in files.items():
        target_path = staging_dir / rel
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8", newline="\n")
    return publish.StagedRowResult(
        staging_dir=staging_dir,
        row_visited=set(),
        row_changed_files=None,
        row_removed_files=set(),
        row_published_files={Path(rel) for rel in files},
        report_text="",
        synced=len(files),
        deleted=0,
    )


def _build_round_fixture(monkeypatch, tmp_path, *, fail_gate: str = None):
    """One-row round: real gate dispatch wrappers and the real `land_diff`, recorded in call order.
    `process_target` is faked to stage one new file."""
    dest_root = tmp_path / "dest-repo"
    _init_git_repo(dest_root)
    dest_a = dest_root / "sub-a"
    rows = [_row_string("row-a", tmp_path / "src-a", dest_a)]
    _wire_common_fakes(monkeypatch, tmp_path, rows)

    record: list = []
    call_log: list = []

    for gate_name in _END_OF_RUN_GATE_NAMES:
        if fail_gate is not None and gate_name == fail_gate:
            monkeypatch.setattr(publish, gate_name, lambda *a, _n=gate_name, **k: (record.append(_n), False)[1])
        else:
            _wrap_and_record(monkeypatch, publish, gate_name, record, call_log)

    publish._bootstrap_engine()
    from percolate import diff_commit

    _wrap_and_record(monkeypatch, diff_commit, "land_diff", record, call_log)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        record.append("process_target:row-a")
        totals.processed += 1
        return _stage_payload(target.dest_dir, {"payload.txt": "published\n"})

    monkeypatch.setattr(publish, "process_target", fake_process_target)

    return dest_root, rows, record, call_log


def test_event_order_stage_gates_land(monkeypatch, tmp_path):
    dest_root, rows, record, _call_log = _build_round_fixture(monkeypatch, tmp_path)

    rc = publish.main(["row-a"])

    assert rc == 0, f"expected a clean round, got rc={rc}"
    assert record[0] == "process_target:row-a", record
    assert "land_diff" in record
    land_idx = record.index("land_diff")
    gate_indices = [record.index(g) for g in _END_OF_RUN_GATE_NAMES if g in record]
    assert set(g for g in _END_OF_RUN_GATE_NAMES if g in record) == set(_END_OF_RUN_GATE_NAMES), record
    assert max(gate_indices) < land_idx, ("every gate must precede the landing", record)
    assert min(gate_indices) > record.index("process_target:row-a"), ("gates read staged rows", record)
    assert land_idx == len(record) - 1, ("nothing the test wraps runs after the landing", record)
    assert "payload.txt" in _git(dest_root, "ls-tree", "-r", "--name-only", "HEAD").stdout.replace("sub-a/", "")


def test_failing_gate_leaves_dest_untouched(monkeypatch, tmp_path):
    dest_root, rows, record, _call_log = _build_round_fixture(
        monkeypatch, tmp_path, fail_gate="dispatch_end_of_run_argv_parity_gate"
    )
    before_mtimes = _mtimes(dest_root)
    before_porcelain = _porcelain(dest_root)
    before_head = _git(dest_root, "rev-parse", "HEAD").stdout

    rc = publish.main(["row-a"])

    assert rc != 0, "a failing gate must not report success"
    assert "land_diff" not in record, ("the landing must never fire when a gate fails", record)
    assert _git(dest_root, "rev-parse", "HEAD").stdout == before_head
    assert _porcelain(dest_root) == before_porcelain == ""
    assert _mtimes(dest_root) == before_mtimes, "no destination file may be touched when a gate fails"


def _root_args(value):
    """Flatten a gate call's value into Path-like root args: a bare `Path`/str, each element of a
    list/tuple/set, or each KEY of a dict (the `*_by_repo_root` shapes)."""
    if isinstance(value, dict):
        for key in value:
            yield from _root_args(key)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _root_args(item)
    elif isinstance(value, (str, Path)):
        yield value


def test_gates_receive_the_staged_union_never_the_dest(monkeypatch, tmp_path):
    """Every end-of-run gate is handed the per-root staged union, never `target.dest_dir`."""
    dest_root, rows, record, call_log = _build_round_fixture(monkeypatch, tmp_path)
    union_roots: list = []
    original_assemble = publish._assemble_root_union

    def _spy(*args, **kwargs):
        union = original_assemble(*args, **kwargs)
        union_roots.append(union.union_root)
        return union

    monkeypatch.setattr(publish, "_assemble_root_union", _spy)

    rc = publish.main(["row-a"])
    assert rc == 0, f"expected a clean round, got rc={rc}"
    assert len(union_roots) == 1

    dest_a = dest_root / "sub-a"
    seen_gate_names = set()
    for name, args, kwargs in call_log:
        if name not in _END_OF_RUN_GATE_NAMES:
            continue
        seen_gate_names.add(name)
        non_root_kwarg_names = {"percolate_root", "out"}
        values_in_scope = list(args) + [v for k, v in kwargs.items() if k not in non_root_kwarg_names]
        root_values = []
        for value in values_in_scope:
            root_values.extend(_root_args(value))
        assert str(dest_a) not in [str(v) for v in root_values], (name, args, kwargs)
        assert str(dest_root) not in [str(v) for v in root_values], (name, args, kwargs)
        assert root_values, (f"{name} carried no Path-like root args to check", args, kwargs)
        assert all(str(union_roots[0]) == str(v) for v in root_values), (name, args, kwargs, root_values)

    assert seen_gate_names == set(_END_OF_RUN_GATE_NAMES), seen_gate_names


def test_process_target_has_no_static_write_into_dest_dir():
    """`process_target` is stage-only: the AST of `process_target` must contain no call to the
    landing and no shutil/os rename-or-copy call whose target names `dest_dir`."""
    source = (_BIN_DIR / "publish.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_BIN_DIR / "publish.py"))

    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "process_target":
            func_node = node
            break
    assert func_node is not None, "process_target not found in publish.py"

    _FORBIDDEN_CALL_NAMES = {
        "land_diff",
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
                call_src = ast.get_source_segment(source, node) or ""
                if "dest_dir" in call_src:
                    violations.append(f"{pair[0]}.{pair[1]}")

    assert not violations, f"process_target contains a write into dest_dir: {violations}"
