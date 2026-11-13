"""Classifier table: each heavy class across bash and PowerShell, wrapper peels, light mentions, scoped carve-outs."""

import pytest

from coordinator_core.bash_guards._heavy_admission_contract import HeavyClass as H
from coordinator_core.bash_guards._heavy_command_class import classify

pytestmark = pytest.mark.complete

CWD = {"cwd": "."}

HEAVY = [
    ("tsc --noEmit", H.TYPECHECK),
    ("vue-tsc -b", H.TYPECHECK),
    ("npx tsc -p .", H.TYPECHECK),
    ("pnpm typecheck", H.TYPECHECK),
    ("npm run typecheck", H.TYPECHECK),
    ("yarn run typecheck", H.TYPECHECK),
    ("bun run typecheck", H.TYPECHECK),
    ("next build", H.BUILD),
    ("vite build", H.BUILD),
    ("cargo build --release", H.BUILD),
    ("pnpm build", H.BUILD),
    ("pnpm run build", H.BUILD),
    ("npm run build", H.BUILD),
    ("pytest", H.TEST_TIER),
    ("pytest -m slow", H.TEST_TIER),
    ("pnpm test", H.TEST_TIER),
    ("python -m pytest", H.TEST_TIER),
    ("UnrealBuildTool.exe Foo Win64", H.UE),
    ("RunUAT.bat BuildCookRun", H.UE),
    ("python project_rag_cli.py reindex --purge", H.REINDEX),
    ("python ../example-retrieval-repo/project_rag_cli.py index", H.REINDEX),
    # PowerShell dialect and wrapper peels
    ("& tsc --noEmit", H.TYPECHECK),
    ('pwsh -Command "tsc --noEmit"', H.TYPECHECK),
    ('powershell -NoProfile -Command "& pnpm typecheck"', H.TYPECHECK),
    ("cmd /c pnpm run build", H.BUILD),
    ("bash -c 'cargo build'", H.BUILD),
    ("env FOO=1 tsc -p .", H.TYPECHECK),
    ("cd x && vite build", H.BUILD),
    ('pwsh -Command "& .\\Build.bat"', H.UE),
    ("pytest tests/a.py::t && tsc", H.TYPECHECK),
]

LIGHT = [
    "grep tsc src/x.ts",
    'echo "cargo build"',
    "git log -- build",
    "ls",
    "git status",
    "cat docs/typecheck.md",
    "pnpm install",
    "cargo check",
    "",
]

SCOPED = [
    "pytest tests/test_a.py::test_x",
    "pytest coordinator_core/bash_guards/tests/test_heavy_command_class.py",
]

WATCH_AND_FANOUT = [
    ("vitest", H.TEST_TIER),
    ("vitest watch", H.TEST_TIER),
    ("pnpm vitest --watch", H.TEST_TIER),
    ("jest --watch", H.TEST_TIER),
    ("jest --watchAll", H.TEST_TIER),
    ("pnpm test --watch", H.TEST_TIER),
    ("tsc -w", H.TYPECHECK),
    ("tsc --watch -p .", H.TYPECHECK),
    ("pnpm -r build", H.BUILD),
    ("pnpm -r lint", H.BUILD),
    ("pnpm --recursive test", H.TEST_TIER),
    ("npm run lint --workspaces", H.BUILD),
]


@pytest.mark.parametrize("cmd,expected", HEAVY)
def test_heavy_classes(cmd, expected):
    c = classify(cmd, CWD)
    assert c.heavy_class is expected
    assert c.scoped is False


@pytest.mark.parametrize("cmd", LIGHT)
def test_mentions_stay_light(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is None
    assert c.scoped is False


@pytest.mark.parametrize("cmd", SCOPED)
def test_scoped_carve_out(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is None
    assert c.scoped is True


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -n 4 tests/a.py",
        "pytest -n4 tests/a.py::t",
        "pytest -n auto tests/a.py",
        "pytest --numprocesses=2 tests/a.py",
        "pytest --numprocesses 8 tests/a.py",
        "python -m pytest -n 4 tests/a.py",
        "python -m pytest tests/a.py -n logical",
        "cd /x && python -m pytest -n 4 tests/a.py",
        "pytest -n 0 tests/a.py -n 4",
        "poetry run pytest -n 4 tests/a.py",
        "timeout 600 pytest -n 4 tests/a.py",
        "FOO=1 python -u -m pytest -n 4 tests/a.py",
    ],
)
def test_xdist_run_is_heavy_even_when_scoped(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is H.TEST_TIER
    assert c.scoped is False
    assert c.bounded_verify is False


@pytest.mark.parametrize(
    "cmd",
    ["echo pytest -n 4 tests/a.py", "tail -f pytest -n 4", "grep pytest -n 4 log.txt"],
)
def test_pytest_n_as_an_argument_not_the_runner_is_light(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is None


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -n 1 tests/a.py",
        "pytest -n0 tests/a.py",
        "python -m pytest -n 0 tests/a.py::t",
        "pytest tests/a.py -k name",
        "pytest -n 4 tests/a.py -n 1",
    ],
)
def test_single_or_no_worker_scoped_run_stays_scoped(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is None
    assert c.scoped is True


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest a/test_a.py b/test_b.py",
        "python -m pytest -q -rfE -p no:cacheprovider bin/tests/a.py coordinator/bin/b.py",
        "cd /x && pytest a.py b.py -k name",
        "pytest -- a.py b.py",
    ],
)
def test_plain_few_file_pytest_is_scoped_and_marked_bare(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is None
    assert c.scoped is True
    assert c.bare_pytest is True


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("python -I -c 'print(1)'", True),
        ("python3 -I -c 'import json'", True),
        ("python scripts/parse.py in.pdf", True),
        ("python -", True),
        ("cat x | python -", True),
        ("pytest a/test_a.py", True),
        ("cd /x && python -m pytest -q a.py 2>&1 | tail -5", True),
        ("python -m pytest", True),
        ("pwsh -Command \"python -m pytest a.py\"", True),
        ("python x.py && tsc --noEmit", False),
        ("python x.py; pnpm build", False),
        ("git status", False),
        ("echo python", False),
        ("", False),
    ],
)
def test_runs_python_only(cmd, expected):
    from coordinator_core.bash_guards._heavy_command_class import runs_python_only

    assert runs_python_only(cmd, None, ".") is expected


def test_an_inline_probe_is_light_for_every_caller():
    for cmd in ("python -", "python3 -c 'print(1)'", "python <<'EOF'\nprint(1)\nEOF"):
        c = classify(cmd, CWD)
        assert c.heavy_class is None and c.scoped is False


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest a/test_a.py",
        "python -m pytest -q -p no:cacheprovider a/test_a.py::t",
        "pytest -k one -m slow a/test_a.py",
        "python -m coordinator_core.testing.scoped_pytest --allow a.py b.py -- a.py b.py",
        "echo pytest a.py b.py",
    ],
)
def test_single_target_and_launcher_and_mentions_are_not_heavy(cmd):
    assert classify(cmd, CWD).heavy_class is None


def test_remedy_names_the_launcher():
    from coordinator_core.bash_guards._heavy_command_class import SCOPED_PYTEST_REMEDY

    assert SCOPED_PYTEST_REMEDY.startswith("scoped-pytest ")
    assert "python -m" not in SCOPED_PYTEST_REMEDY
    inventory = (_REPO / "docs/install/bin-inventory.json").read_text(encoding="utf-8")
    assert '"scoped-pytest"' in inventory
    assert classify("scoped-pytest --allow a.py -- a.py::t", CWD).heavy_class is None
    assert classify("pytest", CWD).heavy_class is H.TEST_TIER


def test_bare_tier_and_marker_forms_stay_heavy():
    for cmd in ("pytest", "pytest -m slow", "vitest run", "pnpm test"):
        assert classify(cmd, CWD).heavy_class is H.TEST_TIER


def test_background_mirrors_tool_input():
    assert classify("tsc", {"run_in_background": True}).background is True
    assert classify("tsc", {}).background is False
    assert classify("ls", {"run_in_background": True}).background is True


def test_non_string_command_is_light():
    assert classify(None, None).heavy_class is None
    assert classify(42, {}).heavy_class is None


def test_ue_precedence_over_scoped_runner():
    assert classify("pytest tests/a.py::t; RunUAT.bat x", CWD).heavy_class is H.UE


@pytest.mark.parametrize("cmd,expected", WATCH_AND_FANOUT)
def test_watch_and_every_workspace_runs_are_heavy(cmd, expected):
    assert classify(cmd, CWD, ".", 4).heavy_class is expected


@pytest.mark.parametrize(
    "cmd",
    ["vitest run src/a.test.ts", "vitest run src/a.test.ts --maxWorkers=8", "npx vitest run a.test.ts"],
)
def test_a_scoped_vitest_file_with_uncapped_workers_is_heavy(cmd, tmp_path):
    assert classify(cmd, None, str(tmp_path), 4).heavy_class is H.TEST_TIER


@pytest.mark.parametrize(
    "cmd",
    [
        "vitest run src/a.test.ts --maxWorkers=2",
        "vitest run --maxWorkers 4 src/b.spec.tsx",
        "vitest run --project ddct packages/x/a.test.ts --maxWorkers=2",
        "npx vitest run a.test.ts --maxWorkers=1",
        "pnpm exec vitest run a.test.ts --maxWorkers=1",
    ],
)
def test_vitest_capped_at_or_under_the_box_cap_is_not_heavy(cmd, tmp_path):
    c = classify(cmd, None, str(tmp_path), 4)
    assert c.heavy_class is None and c.scoped is True


def test_vitest_is_heavy_when_the_cap_is_unconfigured(tmp_path):
    assert classify("vitest run a.test.ts --maxWorkers=1", None, str(tmp_path), None).heavy_class is H.TEST_TIER


def test_a_watched_vitest_run_stays_heavy_even_when_capped(tmp_path):
    assert classify("vitest --maxWorkers=1", None, str(tmp_path), 4).heavy_class is H.TEST_TIER


@pytest.mark.parametrize(
    "config,expected",
    [
        ("export default { test: { maxWorkers: 4 } }", None),
        ("export default { test: { maxWorkers: 16 } }", H.TEST_TIER),
        ("export default { test: { maxWorkers: cores() } }", None),
        ("export default { test: {} }", H.TEST_TIER),
    ],
)
def test_repo_vitest_config_pin_decides(config, expected, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "vitest.config.ts").write_text(config, encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    assert classify("vitest run a.test.ts", None, str(sub), 4).heavy_class is expected


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("npx --no-install tsc --version", H.TYPECHECK),
        ("npx -y -p typescript tsc --noEmit", H.TYPECHECK),
        ("pnpm exec tsc -p .", H.TYPECHECK),
        ("pnpm dlx tsc", H.TYPECHECK),
        ("npx --yes vitest", H.TEST_TIER),
        ("npx prettier --check .", None),
        ("pnpm exec eslint .", None),
    ],
)
def test_a_launcher_flag_never_hides_the_binary(cmd, expected):
    assert classify(cmd, CWD, ".", 4).heavy_class is expected


@pytest.mark.parametrize(
    "cmd",
    [
        "vitest run --maxWorkers=2",
        "vitest run audit --maxWorkers=2",
        "vitest run src/a.test.ts audit --maxWorkers=2",
        "vitest run src/ --maxWorkers=2",
    ],
)
def test_a_capped_vitest_run_without_only_file_paths_stays_heavy(cmd, tmp_path):
    assert classify(cmd, None, str(tmp_path), 4).heavy_class is H.TEST_TIER


@pytest.mark.parametrize(
    "cmd,noemit",
    [
        ("tsc --noEmit", True),
        ("pnpm exec tsc --noEmit -p .", True),
        ("npx --no-install tsc --noEmit true", True),
        ("tsc --noEmit=true", True),
        ("tsc", False),
        ("tsc --noEmit false", False),
        ("tsc --noEmit=false", False),
        ("tsc --noEmit --watch", False),
        ("tsc -b --noEmit", False),
        ("pnpm typecheck", False),
        ("tsc --noEmit; pnpm build", False),
        ("tsc --noEmit && pnpm typecheck", False),
        ("tsc --noEmit && tsc --noEmit -p packages/a", True),
    ],
)
def test_noemit_tsc_marks_only_commands_whose_every_heavy_segment_is_one(cmd, noemit, tmp_path):
    assert classify(cmd, None, str(tmp_path)).noemit_tsc is noemit


def test_a_capped_scoped_vitest_beside_noemit_tsc_keeps_the_mark(tmp_path):
    c = classify("vitest run a.test.ts --maxWorkers=1 && tsc --noEmit", None, str(tmp_path), 4)
    assert c.heavy_class is H.TYPECHECK and c.noemit_tsc is True


def test_an_uncapped_vitest_beside_noemit_tsc_drops_the_mark(tmp_path):
    c = classify("vitest run a.test.ts && tsc --noEmit", None, str(tmp_path), 4)
    assert c.heavy_class is H.TYPECHECK and c.noemit_tsc is False


def _pkg(tmp_path, script):
    (tmp_path / ".git").mkdir()
    (tmp_path / "package.json").write_text('{"scripts": {"typecheck": %s}}' % __import__("json").dumps(script))
    return str(tmp_path)


@pytest.mark.parametrize("script", ["tsc --noEmit", "tsc -p tsconfig.json --noEmit", "npx tsc --noEmit"])
def test_a_package_typecheck_that_is_only_noemit_tsc_is_noemit(tmp_path, script):
    c = classify("pnpm typecheck", None, _pkg(tmp_path, script), 4)
    assert c.heavy_class is H.TYPECHECK and c.noemit_tsc


@pytest.mark.parametrize(
    "cmd,script",
    [
        ("pnpm typecheck", "tsc --noEmit && pnpm build"),
        ("pnpm typecheck", "tsc -b"),
        ("pnpm typecheck", "tsc --noEmit --watch"),
        ("pnpm -r typecheck", "tsc --noEmit"),
        ("pnpm typecheck", "turbo run typecheck"),
    ],
)
def test_an_opaque_or_heavier_package_typecheck_is_not_noemit(tmp_path, cmd, script):
    c = classify(cmd, None, _pkg(tmp_path, script), 4)
    assert c.heavy_class is not None and not c.noemit_tsc


@pytest.mark.parametrize("cmd", ["pnpm run typecheck", "pnpm typecheck", "npm run typecheck"])
def test_an_executor_admissible_package_typecheck_run_forms_are_noemit(tmp_path, cmd):
    c = classify(cmd, None, _pkg(tmp_path, "tsc --noEmit"), 4)
    assert c.heavy_class is H.TYPECHECK and c.noemit_tsc


@pytest.mark.parametrize(
    "cmd",
    [
        "pnpm run typecheck --watch",
        "pnpm typecheck -w",
        "pnpm run typecheck -- --watch",
        "pnpm -r run typecheck",
        "pnpm run typecheck --recursive",
        "npm run typecheck --workspaces",
        "pnpm run typecheck --watchAll",
    ],
)
def test_watch_and_recursive_package_typecheck_forms_are_not_noemit(tmp_path, cmd):
    c = classify(cmd, None, _pkg(tmp_path, "tsc --noEmit"), 4)
    assert c.heavy_class is not None and not c.noemit_tsc


R1_OVERLAY_CMD = (
    "cd /fixture/likeness-prototype; python -m likeness.dense_map_2d.overlay "
    "--run-key 2026-10-27-hrn-photo-run --photos-key fifa-photos "
    "--out-key 2026-10-27-dv01-end/png-kane > scratch/dv01-c3-overlay.log 2>&1"
)


def test_the_r1_overlay_command_is_heavy():
    assert classify(R1_OVERLAY_CMD, None, ".", 4).heavy_class is not None


@pytest.mark.parametrize(
    "cmd",
    ["python -m pytest a.py", "python -m json.tool", "python -m likeness.dense_map_2d.other"],
)
def test_python_module_controls_stay_light(cmd):
    assert classify(cmd, None, ".", 4).heavy_class is None


def test_a_package_typecheck_without_a_package_json_is_not_noemit(tmp_path):
    (tmp_path / ".git").mkdir()
    c = classify("pnpm typecheck", None, str(tmp_path), 4)
    assert c.heavy_class is H.TYPECHECK and not c.noemit_tsc


# --- admit_before_spawn: the shared admit-before-spawn chokepoint -------------------------------

import contextlib
import re
import types
from pathlib import Path

from coordinator_core.bash_guards import _heavy_command_class as _hcc

_GIB = 1 << 30
_REPO = Path(__file__).resolve().parents[3]


def _fake_host(avail_gib, recorded=None, reserved_gib=0, boom=False):
    log = []
    status = _hcc._load_box_memory_host().MemoryStatus(
        avail_phys=int(avail_gib * _GIB), total_phys=1 << 40, commit_used=0, commit_limit=1 << 50
    )

    def read_status():
        if boom:
            raise OSError("unreadable")
        return status

    def admit(peak, st):
        need = peak * 1.25
        return types.SimpleNamespace(
            admit=need <= st.avail_phys and st.commit_used + peak <= st.commit_limit, need_avail=need
        )

    host = types.SimpleNamespace(
        GIB=_GIB,
        admission_file=lambda: "adm",
        read_recorded_peak=lambda path, key: recorded,
        read_memory_status=read_status,
        reserved_bytes=lambda path: int(reserved_gib * _GIB),
        admit=admit,
        admission_lock=lambda path: contextlib.nullcontext(),
        register_command=lambda path, key, pid, peak: log.append((key, pid, peak)),
    )
    return host, log


def test_admit_before_spawn_admits_when_peak_times_1_25_fits():
    host, _ = _fake_host(avail_gib=10)
    v = _hcc.admit_before_spawn("scip-rebuild", 4.0, host=host)
    assert v.admitted and v.peak_gib == 4.0


def test_admit_before_spawn_refuses_if_reservation_cannot_be_written():
    host, _ = _fake_host(avail_gib=10)
    def failed_write(*args):
        raise OSError("reservation store unavailable")
    host.register_command = failed_write
    verdict = _hcc.admit_before_spawn("codex-worker", 1, host=host, register_pid=123)
    assert not verdict.admitted
    assert verdict.reason == "memory reservation write failed"


def test_admit_before_spawn_holds_when_headroom_is_short():
    host, _ = _fake_host(avail_gib=4.5)
    v = _hcc.admit_before_spawn("scip-rebuild", 4.0, host=host)
    assert not v.admitted and v.reason.startswith("HOLD scip-rebuild")


def test_hold_reason_names_top_holders_on_one_path_free_line():
    host, _ = _fake_host(avail_gib=4.5)
    host.top_holders = lambda path, n: [
        {"session_id": "box-memory-snapshot", "run": "test-runner-pytest:7", "pid": 7,
         "image": "sys32\\conhost.exe", "bytes": 2 * _GIB}]
    r = _hcc.admit_before_spawn("scip-rebuild", 4.0, host=host).reason
    assert "holders: box-memory-snapshot test-runner-pytest:7 pid=7 conhost.exe 2.0GiB" in r
    assert "\n" not in r and "\\" not in r and "/" not in r


def test_hold_reason_degrades_without_top_holders_or_when_it_raises():
    host, _ = _fake_host(avail_gib=4.5)
    assert "holders" not in _hcc.admit_before_spawn("k", 4.0, host=host).reason
    host.top_holders = lambda path, n: 1 / 0
    r = _hcc.admit_before_spawn("k", 4.0, host=host).reason
    assert r.startswith("HOLD k") and "holders" not in r


def test_admit_before_spawn_nets_peer_reservations_and_honours_larger_recorded_peak():
    host, _ = _fake_host(avail_gib=10, reserved_gib=6)
    assert not _hcc.admit_before_spawn("k", 4.0, host=host).admitted
    host, _ = _fake_host(avail_gib=10, recorded=9.0)
    v = _hcc.admit_before_spawn("k", 4.0, host=host)
    assert not v.admitted and v.peak_gib == 9.0


def test_admit_before_spawn_refuses_on_an_unreadable_reading():
    host, _ = _fake_host(avail_gib=99, boom=True)
    v = _hcc.admit_before_spawn("k", 1.0, host=host)
    assert not v.admitted and "unreadable" in v.reason


def _denied(*args):
    raise PermissionError("Access is denied")


def _serve_engine(monkeypatch, engine_host, calls):
    """Stub the warm pipe so box_memory.* requests run `_admit_local` over a readable host."""
    def dispatch(msg, **kw):
        calls.append((msg["method"], kw))
        p = msg["params"]
        if msg["method"] == "box_memory.admit":
            v = _hcc._admit_local(p["key"], p["default_peak_gib"], engine_host, p.get("register_pid"))
            return {"jsonrpc": "2.0", "id": 1, "result": {
                "admitted": v.admitted, "reason": v.reason, "key": v.key, "peak_gib": v.peak_gib,
                "measured": True}}
        if msg["method"] == "box_memory.register":
            ok = _hcc.register_spawn(p["key"], p["peak_gib"], p["pid"], engine_host)
            return {"jsonrpc": "2.0", "id": 1, "result": {"ok": ok}}
        engine_host.release_command("adm", p["key"], p["pid"])
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", dispatch)


@pytest.mark.parametrize("raiser", ["read_memory_status", "reserved_bytes", "admission_lock"])
def test_engine_path_admits_with_a_measured_verdict_and_a_reservation(raiser, monkeypatch):
    local, _ = _fake_host(avail_gib=99)
    setattr(local, raiser, _denied)
    engine, log = _fake_host(avail_gib=99)
    calls = []
    _serve_engine(monkeypatch, engine, calls)
    v = _hcc.admit_before_spawn("k", 1.0, host=local, register_pid=9)
    assert v.admitted and "advisory" not in v.reason and v.reason.startswith("ADMIT k")
    assert log == [("k", 9, 1.0)]
    assert calls == [("box_memory.admit", {"hook_path": True})]


def test_engine_path_refuses_with_a_hold_when_the_engine_measures_too_little(monkeypatch):
    local, _ = _fake_host(avail_gib=99)
    local.read_memory_status = _denied
    engine, log = _fake_host(avail_gib=2)
    _serve_engine(monkeypatch, engine, [])
    v = _hcc.admit_before_spawn("k", 4.0, host=local, register_pid=9)
    assert not v.admitted and v.reason.startswith("HOLD k") and log == []


def test_engine_path_error_envelope_refuses_not_advisory(monkeypatch):
    local, _ = _fake_host(avail_gib=99)
    local.read_memory_status = _denied
    monkeypatch.setattr(
        "coordinator_core.warm.client.try_warm_dispatch",
        lambda msg, **kw: {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "x"}})
    v = _hcc.admit_before_spawn("k", 1.0, host=local, register_pid=9)
    assert not v.admitted and v.reason == "memory unreadable via engine (-32603)"


def test_engine_path_register_and_release_route_to_the_engine(monkeypatch):
    local, _ = _fake_host(avail_gib=99)
    local.register_command = _denied
    local.release_command = _denied
    engine, log = _fake_host(avail_gib=99)
    released = []
    engine.release_command = lambda path, key, pid: released.append((key, pid))
    calls = []
    _serve_engine(monkeypatch, engine, calls)
    assert _hcc.register_spawn("k", 2.0, 9, local) is True
    _hcc.release_spawn("k", 9, local)
    assert log == [("k", 9, 2.0)] and released == [("k", 9)]
    assert [c[0] for c in calls] == ["box_memory.register", "box_memory.release"]


def test_default_path_makes_no_warm_dispatch_call(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("warm dispatch on the default path")

    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", fail)
    host, log = _fake_host(avail_gib=99)
    assert _hcc.admit_before_spawn("k", 1.0, host=host, register_pid=9).admitted
    assert _hcc.register_spawn("k", 1.0, 9, host) and log[-1] == ("k", 9, 1.0)
    host.release_command = lambda *a: None
    _hcc.release_spawn("k", 9, host)
    host2, _ = _fake_host(avail_gib=99, boom=True)
    assert not _hcc.admit_before_spawn("k", 1.0, host=host2).admitted


@pytest.mark.parametrize("raiser", ["read_memory_status", "reserved_bytes", "admission_lock"])
def test_a_sandbox_permission_error_with_the_engine_unreachable_admits_with_an_advisory(raiser, monkeypatch):
    host, log = _fake_host(avail_gib=99)
    setattr(host, raiser, _denied)
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", lambda msg, **kw: None)
    rows = []
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_sandbox_advisory_admit", lambda **kw: rows.append(kw))
    v = _hcc.admit_before_spawn("k", 1.0, host=host, register_pid=9)
    assert v.admitted and "advisory" in v.reason and "memory unreadable (" not in v.reason.split("advisory")[0]
    assert rows == [{"key": "k", "pid": 9}] and log == []


def test_a_non_permission_error_still_refuses_where_permission_error_admits(monkeypatch):
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", lambda msg, **kw: None)
    host, _ = _fake_host(avail_gib=99)
    host.reserved_bytes = lambda path: 1 / 0
    v = _hcc.admit_before_spawn("k", 1.0, host=host, register_pid=9)
    assert not v.admitted and v.reason == "memory unreadable (ZeroDivisionError)"


def test_record_sandbox_advisory_admit_appends_one_row_and_never_raises(tmp_path):
    from coordinator_core.warm import telemetry

    telemetry.record_sandbox_advisory_admit(engine_root=tmp_path, key="k", pid=9)
    path = telemetry.sandbox_advisory_admit_path(tmp_path)
    import json

    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["key"] == "k" and row["pid"] == 9 and "ts" in row
    telemetry.record_sandbox_advisory_admit(engine_root=tmp_path / "x" / ("y" * 5000))


def test_host_without_admission_lock_reaches_a_memory_verdict():
    host, log = _fake_host(avail_gib=10)
    del host.admission_lock
    v = _hcc.admit_before_spawn("k", 4.0, host=host, register_pid=9)
    assert v.admitted and log == [("k", 9, 4.0)]
    host, log = _fake_host(avail_gib=1)
    del host.admission_lock
    v = _hcc.admit_before_spawn("k", 4.0, host=host, register_pid=9)
    assert not v.admitted and v.reason.startswith("HOLD k") and log == []


def test_host_without_admission_lock_still_refuses_an_unreadable_reading():
    host, _ = _fake_host(avail_gib=99, boom=True)
    del host.admission_lock
    v = _hcc.admit_before_spawn("k", 1.0, host=host, register_pid=9)
    assert not v.admitted and "unreadable" in v.reason


def _patch_wait(monkeypatch, result):
    from coordinator_core.group_em import slot_wait

    seen = []
    monkeypatch.setattr(
        slot_wait, "request_and_wait", lambda sid, run, mb, **kw: seen.append((sid, run, mb, kw)) or result
    )
    return seen


def test_hold_over_threshold_waits_for_a_grant_and_admits_on_it(monkeypatch):
    seen = _patch_wait(monkeypatch, {"admitted": True, "reason": "granted"})
    host, log = _fake_host(avail_gib=1)
    v = _hcc.admit_before_spawn("k", 4.0, host=host, register_pid=5, wait_kw={"timeout_s": 3}, session_id="s1")
    assert v.admitted and seen == [("s1", "k", 4096.0, {"timeout_s": 3})] and log == [("k", 5, 4.0)]


def test_hold_keeps_the_refusal_without_a_grant_or_wait_or_session(monkeypatch):
    seen = _patch_wait(monkeypatch, {"admitted": False, "reason": "timeout"})
    host, _ = _fake_host(avail_gib=1)
    v = _hcc.admit_before_spawn("k", 4.0, host=host, wait_kw={}, session_id="s1")
    assert not v.admitted and "HOLD k" in v.reason and "timeout" in v.reason
    n = len(seen)
    assert not _hcc.admit_before_spawn("k", 4.0, host=host, session_id="s1").admitted  # wait_kw None
    assert not _hcc.admit_before_spawn("k", 1.0, host=host, wait_kw={}, session_id="s1").admitted  # under 2 GiB
    from coordinator_core.session import core

    monkeypatch.setattr(core, "resolve_session_id", lambda: "")
    assert not _hcc.admit_before_spawn("k", 4.0, host=host, wait_kw={}).admitted  # no session
    assert len(seen) == n


def test_register_pid_reserves_only_on_admit():
    host, log = _fake_host(avail_gib=10)
    _hcc.admit_before_spawn("k", 4.0, host=host, register_pid=77)
    assert log == [("k", 77, 4.0)]
    host, log = _fake_host(avail_gib=1)
    _hcc.admit_before_spawn("k", 4.0, host=host, register_pid=77)
    assert log == []


def test_real_host_module_loads_and_exposes_the_reused_admit():
    host = _hcc._load_box_memory_host()
    assert callable(host.admit) and callable(host.reserved_bytes)


def _publish_tool():
    import importlib.util

    path = _REPO / "coordinator" / "bin" / "publish-repo-bundle-at-ceremony.py"
    spec = importlib.util.spec_from_file_location("publish_repo_bundle_x1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_publish_scip_rebuild_hold_never_spawns_and_publish_step_is_not_gated(monkeypatch, tmp_path):
    tool = _publish_tool()
    spawned = []
    monkeypatch.setattr(tool.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(
        _hcc, "admit_before_spawn",
        lambda key, peak, **kw: _hcc.SpawnAdmission(False, "HOLD scip-rebuild: test", key, peak),
    )
    code, _out, err = tool._run_and_wait(["cli", "scip-rebuild", "--ceremony", "x"], tmp_path, 5)
    assert code is None and err.startswith("admission: HOLD") and spawned == []
    tool._run_and_wait(["cli", "publish-repo-bundle"], tmp_path, 5)
    assert spawned == [["cli", "publish-repo-bundle"]]


def test_publish_run_reports_a_hold_not_a_timeout(monkeypatch, tmp_path):
    tool = _publish_tool()
    cli = tmp_path / "cli"
    cli.write_text("")
    monkeypatch.setattr(tool, "_run_and_wait", lambda a, c, t: (None, "", "admission: HOLD scip-rebuild: x"))
    line = tool.run("x", tmp_path, cli_override=str(cli))
    assert "not admitted" in line and "timed out" not in line


# Ratchet: a heavy spawn site that bypasses the shared helper fails here. A site not yet routed is
# named in _UNROUTED with its owner; the entry is deleted when the site routes, never extended.
_SPAWN = re.compile(r"Popen\(|subprocess\.(run|check_output|check_call|call)\(|_run_with_group_cleanup\(")
_SCIP = re.compile(r"""["']scip-rebuild["']""")
_PYTEST_N = re.compile(r"""["'](-n|--numprocesses)["']""")
#: scip-rebuild-at-ceremony.py is gated one level up: close_background admits it through
#: post_close_admission before the launcher runs.
_GATED_UPSTREAM = {"coordinator/bin/scip-rebuild-at-ceremony.py"}
_UNROUTED: set = set()


def _heavy_spawn_sites():
    sites = {}
    for root in ("coordinator_core", "coordinator/bin"):
        for p in (_REPO / root).rglob("*.py"):
            if "tests" in p.parts or p.name.startswith("test_"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if not _SPAWN.search(text):
                continue
            xdist_site = "pytest" in text and _PYTEST_N.search(text) and "xdist" in text.lower()
            if _SCIP.search(text) or xdist_site or "capped_worker_count" in text or "_cap_workers" in text:
                sites[p.relative_to(_REPO).as_posix()] = text
    return sites


def test_every_heavy_spawn_site_routes_through_admit_before_spawn():
    bypass = sorted(
        rel for rel, text in _heavy_spawn_sites().items()
        if "admit_before_spawn" not in text and rel not in _GATED_UPSTREAM and rel not in _UNROUTED
    )
    assert bypass == [], "heavy spawn site bypasses admit_before_spawn: %s" % bypass


def test_the_ratchet_still_sees_its_known_sites_and_the_unrouted_list_has_no_stale_entries():
    sites = _heavy_spawn_sites()
    assert "coordinator/bin/publish-repo-bundle-at-ceremony.py" in sites
    assert "admit_before_spawn" in sites["coordinator/bin/publish-repo-bundle-at-ceremony.py"]
    assert _UNROUTED <= set(sites)
    assert all("admit_before_spawn" not in sites[rel] for rel in _UNROUTED)
    close = (_REPO / "coordinator_core/ops/ceremony/close_background.py").read_text(encoding="utf-8")
    assert "admission.admit(" in close


# Widened ratchet: EVERY subprocess spawn or detached launch in ops code and bin scripts is either
# routed through admit_before_spawn, supervised by a routed sequencer, or named here with why it
# is not a heavy-memory launch. A new spawn site fails until it lands in one of the three.
_ANY_SPAWN = re.compile(
    r"Popen\(|start_new_session|CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS|_windows_detached_flags"
)
#: A detached launcher whose child is a routed sequencer: launcher -> sequencer that admits,
#: leases and binds the heavy child to itself.
_SUPERVISED = {
    "coordinator/bin/scip-rebuild-at-ceremony.py": "coordinator/bin/publish-repo-bundle-at-ceremony.py",
}
_NOT_HEAVY = {
    "coordinator_core/ops/agent_pipe.py": "detached relay worker over one vendor CLI (agy or codex), network-bound, not a memory-heavy job",
    "coordinator_core/ops/codex_runner.py": "detached worker over one codex or agy CLI run, network-bound, not a memory-heavy job",
    "coordinator_core/ops/app_session.py": "app launch, not a memory-heavy job",
    "coordinator_core/ops/_app_session_validate_run.py": "app launch, not a memory-heavy job",
    "coordinator_core/ops/ceremony/detached_spawn.py": "light housekeeping scripts",
    "coordinator_core/ops/ceremony/git_native.py": "git subprocess",
    "coordinator_core/ops/emit/enrich.py": "git log subprocess",
    "coordinator_core/ops/dispatch_emit/terminal_commit.py": "detached launcher whose child is the memory-admitted structural_index_refresh_admitted",
    "coordinator_core/ops/dispatch_emit/tranche_chain.py": "detached launcher whose child only emits the next tranche and fires its workflow runner, admitted by the fire op",
    "coordinator_core/ops/rag_freshness.py": "detached launch of example-retrieval-repo's own remedy command, queued once per TTL marker, admitted by example-retrieval-repo itself",
    "coordinator_core/ops/session/safe_commit_offer.py": "light detached offer",
    "coordinator_core/ops/workflow_fire/fire.py": "workflow runner launch, admitted by the fire op",
    "coordinator/bin/lib/claude_author_control/relay_posix.py": "relay shim",
    "coordinator/bin/lib/session_detach.py": "detach helper, spawns nothing itself",
    "coordinator/bin/lib/unimatrix_box_mux.py": "ssh relay channel and broker, network-bound, not a memory-heavy job",
    "coordinator/bin/refresh-plugin-live-install.py": "file-sync tooling",
    "coordinator/bin/structural-index-refresh.py": "admitted upstream by close_background post_close_admission",
    "coordinator/bin/unimatrix-agy-hook-launch.py": "detached agy relay leg, network-bound, not a memory-heavy job",
    "coordinator/bin/unimatrix-agy-hook-pretool.py": "detached agy relay leg launcher, network-bound, not a memory-heavy job",
    "coordinator/bin/unimatrix-autolink.py": "detached ssh bridge supervisors, network-bound, not a memory-heavy job",
    "coordinator/bin/unimatrix-codex-launch.py": "codex app-server daemon launch, not a memory-heavy job",
    "coordinator/bin/unimatrix-file.py": "ssh relay file leg, network-bound, not a memory-heavy job",
    "coordinator/bin/unimatrix-install.py": "claude --version probe, light",
    "coordinator/bin/unimatrix-roster.py": "ssh bridge supervisor, network-bound, not a memory-heavy job",
    "coordinator/bin/validate-fast-and-packageability.py": "wall-budgeted gate (gate_budget)",
    "coordinator/bin/workday-complete-step1-validate.py": "wall-budgeted gate (gate_budget)",
}


def _spawn_site_files():
    out = {}
    for root in ("coordinator_core/ops", "coordinator/bin"):
        for p in (_REPO / root).rglob("*.py"):
            if "tests" in p.parts or p.name.startswith("test_"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if _ANY_SPAWN.search(text):
                out[p.relative_to(_REPO).as_posix()] = text
    return out


def _unaccounted(sites):
    return sorted(
        rel for rel, text in sites.items()
        if "admit_before_spawn" not in text and rel not in _SUPERVISED and rel not in _NOT_HEAVY
    )


def test_every_spawn_and_detached_launch_is_routed_supervised_or_named_not_heavy():
    sites = _spawn_site_files()
    assert "coordinator/bin/scip-rebuild-at-ceremony.py" in sites  # the detached launcher the old ratchet missed
    assert _unaccounted(sites) == []


def test_the_widened_ratchet_refuses_an_unrouted_detached_spawn():
    fake = {"coordinator/bin/new-launcher.py": "subprocess.Popen(argv, start_new_session=True)"}
    assert _unaccounted(fake) == ["coordinator/bin/new-launcher.py"]
    fake["coordinator/bin/new-launcher.py"] += "\nadmit_before_spawn(k, 4.0)"
    assert _unaccounted(fake) == []


def test_a_supervised_launcher_names_a_sequencer_that_admits_and_leases():
    for launcher, sequencer in _SUPERVISED.items():
        assert (_REPO / sequencer).name in (_REPO / launcher).read_text(encoding="utf-8")
        seq = (_REPO / sequencer).read_text(encoding="utf-8")
        assert "admit_before_spawn" in seq and "_claim_lease" in seq and "_bind_child_to_caller" in seq


def test_exemption_lists_carry_no_stale_entries():
    sites = _spawn_site_files()
    assert set(_NOT_HEAVY) <= set(sites) and set(_SUPERVISED) <= set(sites)
    assert not any("admit_before_spawn" in sites[rel] for rel in _NOT_HEAVY)


def test_terminal_commit_not_heavy_exemption_rests_on_an_admitted_child():
    from coordinator_core.ops.dispatch_emit import terminal_commit

    assert "structural_index_refresh_admitted" in terminal_commit._INDEX_REFRESH_CODE
    manifest = (_REPO / "coordinator_core/workday_start/manifest.py").read_text(encoding="utf-8")
    body = manifest.split("def structural_index_refresh_admitted", 1)[1]
    assert body.index("--admit-peak") < body.index("structural-index-refresh")


def _hold(monkeypatch):
    monkeypatch.setattr(
        _hcc, "admit_before_spawn",
        lambda key, peak, **kw: _hcc.SpawnAdmission(False, "HOLD %s: test" % key, key, peak),
    )


def test_xdist_runner_hold_never_spawns_and_a_serial_run_is_not_gated(monkeypatch, tmp_path):
    from coordinator_core.source_edit_gate import runner as rn

    spawned = []

    def fake_popen(argv, **kw):
        spawned.append(argv)
        raise OSError("x")

    monkeypatch.setattr(rn.subprocess, "Popen", fake_popen)
    _hold(monkeypatch)
    # An explicit target: a bare selection is the rootdir, which contains this calling test.
    assert rn._run_with_group_cleanup(["py", "-m", "pytest", "-n", "2", "other_target_test.py"], cwd=str(tmp_path), timeout_s=1) is None
    assert spawned == []
    with pytest.raises(OSError):
        rn._run_with_group_cleanup(["py", "-m", "pytest", "other_target_test.py"], cwd=str(tmp_path), timeout_s=1)
    assert len(spawned) == 1


def test_retest_launch_hold_never_spawns(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import retest_targeted as rt

    spawned = []
    monkeypatch.setattr(rt.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    _hold(monkeypatch)
    with pytest.raises(OSError, match="admission: HOLD"):
        rt._run_argv(["py", "-m", "pytest", "-n", "4", "other_target_test.py"], tmp_path)
    assert spawned == []


def test_red_set_report_hold_never_spawns(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("red_set_report_x1", _REPO / "coordinator" / "bin" / "red-set-report.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    spawned = []
    monkeypatch.setattr(tool.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(tool, "capped_worker_count", lambda n: 2)
    _hold(monkeypatch)
    with pytest.raises(RuntimeError, match="not admitted"):
        tool.run_pytest_json("t", marker_expr=None, workers=2)
    assert spawned == []


def test_the_ratchet_refuses_a_bypass_shape():
    assert _SPAWN.search("subprocess.Popen(argv)") and _SCIP.search('argv = [exe, "scip-rebuild"]')


@pytest.mark.parametrize(
    "cmd", ["python -m pytest a.py b.py", "pytest", "pytest -n 4 a.py", "pytest tests/", "pytest -- tests"]
)
def test_bare_pytest_is_marked(cmd):
    """Any direct pytest other than exactly one file target, xdist included."""
    assert classify(cmd, CWD).bare_pytest is True


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest a/test_a.py",
        "python -m coordinator_core.testing.scoped_pytest --allow a.py b.py -- a.py b.py",
        "echo pytest a.py b.py",
        "tsc --noEmit",
    ],
)
def test_scoped_or_non_pytest_is_not_marked_bare(cmd):
    assert classify(cmd, CWD).bare_pytest is False


def _trimming_host(readings_gib, trim_result="system cache 40.0 -> 2.0 GB"):
    """A fake host whose available memory steps through `readings_gib`, one per memory read."""
    host, log = _fake_host(avail_gib=readings_gib[0])
    reads = iter(readings_gib)
    status_cls = _hcc._load_box_memory_host().MemoryStatus
    host.read_memory_status = lambda: status_cls(
        avail_phys=int(next(reads) * _GIB), total_phys=1 << 40, commit_used=0, commit_limit=1 << 50)
    trims = []
    host.trim_file_cache = lambda: trims.append(1) or trim_result
    return host, log, trims


def test_a_hold_the_cache_trim_clears_admits():
    host, log, trims = _trimming_host([3.7, 30.0])
    v = _hcc.admit_before_spawn("fifa-workflow", 4.0, host=host, register_pid=7)
    assert v.admitted and trims == [1] and log == [("fifa-workflow", 7, 4.0)]


def test_a_hold_still_breached_after_the_trim_refuses_after_one_trim():
    host, log, trims = _trimming_host([3.7, 3.9])
    v = _hcc.admit_before_spawn("fifa-workflow", 4.0, host=host, register_pid=7)
    assert not v.admitted and v.reason.startswith("HOLD fifa-workflow") and trims == [1] and log == []


def test_an_admit_never_trims_and_an_unfired_trim_skips_the_remeasure():
    host, _, trims = _trimming_host([10.0])
    assert _hcc.admit_before_spawn("k", 4.0, host=host).admitted and trims == []
    host, _, trims = _trimming_host([3.7], trim_result=None)  # a second read would exhaust the iterator
    v = _hcc.admit_before_spawn("k", 4.0, host=host)
    assert not v.admitted and v.reason.startswith("HOLD k") and trims == [1]


def test_a_commit_only_hold_does_not_trim():
    host, _, trims = _trimming_host([30.0])
    host.admit = lambda peak, st: types.SimpleNamespace(admit=False, need_avail=peak, avail_ok=True)
    assert not _hcc.admit_before_spawn("k", 4.0, host=host).admitted and trims == []
