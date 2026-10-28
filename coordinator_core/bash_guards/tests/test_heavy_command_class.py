"""Classifier table: each heavy class across bash and PowerShell, wrapper peels, light mentions, scoped carve-outs."""

import pytest

from coordinator_core.bash_guards._heavy_admission_contract import HeavyClass as H
from coordinator_core.bash_guards._heavy_command_class import classify

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
def test_plain_multi_target_pytest_is_heavy(cmd):
    c = classify(cmd, CWD)
    assert c.heavy_class is H.TEST_TIER
    assert c.scoped is False
    assert c.bounded_verify is False


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

    assert "coordinator_core.testing.scoped_pytest" in SCOPED_PYTEST_REMEDY


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

    def admit(peak, st, floor=0):
        need = max(peak * 1.25, floor + peak)
        return types.SimpleNamespace(
            admit=need <= st.avail_phys and st.commit_used + peak <= st.commit_limit, need_avail=need
        )

    host = types.SimpleNamespace(
        GIB=_GIB,
        admission_file=lambda: "adm",
        read_recorded_peak=lambda path, key: recorded,
        read_memory_status=read_status,
        reserved_bytes=lambda path: int(reserved_gib * _GIB),
        guard_floor_bytes=lambda: 0,
        admit=admit,
        admission_lock=lambda path: contextlib.nullcontext(),
        register_command=lambda path, key, pid, peak: log.append((key, pid, peak)),
    )
    return host, log


def test_admit_before_spawn_admits_when_peak_times_1_25_fits():
    host, _ = _fake_host(avail_gib=10)
    v = _hcc.admit_before_spawn("scip-rebuild", 4.0, host=host)
    assert v.admitted and v.peak_gib == 4.0


def test_admit_before_spawn_holds_when_headroom_is_short():
    host, _ = _fake_host(avail_gib=4.5)
    v = _hcc.admit_before_spawn("scip-rebuild", 4.0, host=host)
    assert not v.admitted and v.reason.startswith("HOLD scip-rebuild")


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
    "coordinator_core/ops/app_session.py": "app launch, not a memory-heavy job",
    "coordinator_core/ops/_app_session_validate_run.py": "app launch, not a memory-heavy job",
    "coordinator_core/ops/ceremony/detached_spawn.py": "light housekeeping scripts",
    "coordinator_core/ops/ceremony/git_native.py": "git subprocess",
    "coordinator_core/ops/emit/enrich.py": "git log subprocess",
    "coordinator_core/ops/session/safe_commit_offer.py": "light detached offer",
    "coordinator_core/ops/workflow_fire/fire.py": "workflow runner launch, admitted by the fire op",
    "coordinator/bin/lib/claude_author_control/relay_posix.py": "relay shim",
    "coordinator/bin/lib/session_detach.py": "detach helper, spawns nothing itself",
    "coordinator/bin/refresh-plugin-live-install.py": "file-sync tooling",
    "coordinator/bin/structural-index-refresh.py": "admitted upstream by close_background post_close_admission",
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
    assert rn._run_with_group_cleanup(["py", "-m", "pytest", "-n", "2"], cwd=str(tmp_path), timeout_s=1) is None
    assert spawned == []
    with pytest.raises(OSError):
        rn._run_with_group_cleanup(["py", "-m", "pytest"], cwd=str(tmp_path), timeout_s=1)
    assert len(spawned) == 1


def test_retest_launch_hold_never_spawns(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import retest_targeted as rt

    spawned = []
    monkeypatch.setattr(rt.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    _hold(monkeypatch)
    with pytest.raises(OSError, match="admission: HOLD"):
        rt._run_argv(["py", "-m", "pytest", "-n", "4"], tmp_path)
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


@pytest.mark.parametrize("cmd", ["python -m pytest a.py b.py", "pytest", "pytest -n 4 a.py"])
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
