"""guard-heavy-command-admission: each leg's deny and allow, bypass keys, lease discipline, and
registration, driven by fixture payloads and stubbed readers (no host reads)."""

from __future__ import annotations

import ast
import json
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from coordinator_core.bash_guards import _heavy_lease_store as leases
from coordinator_core.bash_guards import guard_heavy_command_admission as guard
from coordinator_core.bash_guards._heavy_admission_contract import (
    GUARD_NAME,
    KEY_FREE_RAM_FLOOR_MB,
    KEY_LEASE_RESERVE_MB,
    KEY_SESSION_BACKGROUND_CAP,
    KEY_SESSION_HEAVY_CAP,
    KEY_VITEST_MAX_WORKERS,
    KEY_WORKER_RSS_CEILING_MB,
    LeaseRecord,
    MemoryReading,
    OVERRIDE_KEYS,
    ProcRow,
)

CLAUDE = ProcRow(pid=100, ppid=1, ctime=10, name="claude.exe")
CALLER = ProcRow(pid=200, ppid=100, ctime=20, name="python.exe")
CONFIG = {
    KEY_FREE_RAM_FLOOR_MB: "1000",
    KEY_SESSION_HEAVY_CAP: "2",
    KEY_SESSION_BACKGROUND_CAP: "3",
    KEY_LEASE_RESERVE_MB: "500",
}


class _Prims:
    def __init__(self, rows: Optional[List[ProcRow]], alive_set=()):
        self.rows = rows
        self.alive_set = set(alive_set)

    def alive(self, pid, ctime):
        return (pid, ctime) in self.alive_set

    def creation_time(self, pid):
        return None

    def parent(self, pid):
        return None

    def image_name(self, pid):
        return None

    def snapshot(self):
        return self.rows


class _Host:
    """Mutable stub host: RAM reading, process table, config."""

    def __init__(self, avail=5000, trusted=True, rows=None, config=None):
        self.avail = avail
        self.trusted = trusted
        self.rows = [CLAUDE, CALLER] if rows is None else rows
        self.config = dict(CONFIG) if config is None else config
        self.ram_reads = 0

    def prims(self):
        return _Prims(self.rows, alive_set={(CLAUDE.pid, CLAUDE.ctime)})

    def reading(self):
        self.ram_reads += 1
        return MemoryReading(self.avail, self.trusted, "stub")


@pytest.fixture()
def host(tmp_path, monkeypatch):
    h = _Host()
    monkeypatch.setattr(leases, "leases_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(guard, "_read_config", lambda: h.config)
    monkeypatch.setattr(guard, "_primitives", h.prims)
    monkeypatch.setattr(guard, "_read_available", h.reading)
    monkeypatch.setattr(guard, "_live_workflow_runs", lambda sid: [])
    monkeypatch.setattr(guard, "_deny_log_path", lambda: tmp_path / "would-deny.jsonl")
    monkeypatch.setattr(guard, "_working_set_mb", lambda pid: h.working_sets.get(pid))
    h.working_sets = {}
    return h


def _payload(command, *, agent_id=None, agent_type=None, background=False, env=None, cwd=None, tool="Bash"):
    p = {
        "tool_name": tool,
        "tool_input": {"command": command, "run_in_background": background},
        "session_id": "sess-heavy",
        "pid": CALLER.pid,
    }
    if cwd is not None:
        p["cwd"] = str(cwd)
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    if env is not None:
        p["env"] = env
    return p


def _log_lines(tmp_path) -> List[dict]:
    path = tmp_path / "would-deny.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _lease_files(tmp_path) -> List[Path]:
    d = tmp_path / "leases"
    return sorted(d.glob("*.json")) if d.exists() else []


class TestLightPath:
    def test_light_foreground_command_reads_nothing(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("light path touched a reader")

        for name in ("_read_config", "_primitives", "_read_available", "_live_workflow_runs"):
            monkeypatch.setattr(guard, name, boom)
        assert guard.check(_payload("git status")) is None
        assert guard.check(_payload("grep tsc README.md", agent_id="deadbeef0123")) is None

    @pytest.mark.parametrize("payload", [None, [], {}, {"tool_name": "Read"}, _payload("")])
    def test_malformed_or_foreign_payload_allows(self, payload):
        assert guard.check(payload) is None

    def test_non_string_command_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": {"command": 5}}) is None


class TestIdentityLeg:
    def test_subagent_tsc_is_denied_naming_the_gate(self, host):
        out = guard.check(_payload("tsc --noEmit", agent_id="deadbeef0123", agent_type="general-purpose"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        text = _reason(out)
        assert GUARD_NAME in text and "identity" in text and "typecheck" in text
        assert host.ram_reads == 0

    @pytest.mark.parametrize(
        "agent_type",
        ["general-purpose", "Explore", "coordinator:executor", "workflow-subagent", "unknown", "", "aliza-0123456789abcdef"],
    )
    def test_unlisted_caller_classes_are_denied(self, host, agent_type):
        out = guard.check(_payload("tsc --noEmit", agent_id="deadbeef0123", agent_type=agent_type))
        assert out is not None and "identity" in _reason(out)

    def test_absent_agent_type_with_agent_id_is_denied(self, host):
        assert guard.check(_payload("tsc --noEmit", agent_id="deadbeef0123")) is not None

    def test_main_thread_is_admitted(self, host):
        assert guard.check(_payload("tsc --noEmit")) is None

    def test_main_thread_with_bare_agent_type_is_admitted(self, host):
        assert guard.check(_payload("tsc --noEmit", agent_type="coordinator:staff-eng")) is None

    def test_listed_persona_is_admitted(self, host):
        assert guard.check(_payload("tsc --noEmit", agent_id="deadbeef0123", agent_type="coordinator:staff-eng")) is None

    def test_listed_persona_inside_a_live_workflow_run_is_denied(self, host, monkeypatch):
        monkeypatch.setattr(guard, "_live_workflow_runs", lambda sid: ["run-1"])
        out = guard.check(_payload("tsc --noEmit", agent_id="deadbeef0123", agent_type="coordinator:staff-eng"))
        assert out is not None and "workflow run" in _reason(out)

    def test_main_thread_never_consults_workflow_runs(self, host, monkeypatch):
        monkeypatch.setattr(guard, "_live_workflow_runs", lambda sid: pytest.fail("consulted"))
        assert guard.check(_payload("tsc --noEmit")) is None

    def test_a_denied_identity_writes_no_lease(self, host, tmp_path):
        guard.check(_payload("tsc --noEmit", agent_id="deadbeef0123", agent_type="general-purpose"))
        assert _lease_files(tmp_path) == []


class TestRamLeg:
    def test_above_floor_admits_and_writes_one_lease(self, host, tmp_path):
        assert guard.check(_payload("pnpm build")) is None
        (lease,) = _lease_files(tmp_path)
        rec = json.loads(lease.read_text(encoding="utf-8"))
        assert (rec["holder_pid"], rec["holder_ctime"]) == (CLAUDE.pid, CLAUDE.ctime)
        assert rec["heavy_class"] == "build"

    def test_below_floor_denies_naming_holders(self, host, tmp_path):
        assert guard.check(_payload("pnpm build")) is None
        host.avail = 900
        out = guard.check(_payload("pnpm build"))
        text = _reason(out)
        assert GUARD_NAME in text and "ram-floor" in text
        assert "pid 100" in text and "build" in text
        assert len(_lease_files(tmp_path)) == 1

    def test_denied_call_writes_no_lease(self, host, tmp_path):
        host.avail = 900
        assert guard.check(_payload("pnpm build")) is not None
        assert _lease_files(tmp_path) == []

    def test_dead_holder_lease_is_reaped_on_the_next_call(self, host, tmp_path):
        leases.write_lease(
            leases.LeaseRecord(
                holder_pid=999, holder_ctime=5, session_pid=999, session_ctime=5,
                heavy_class="build", admitted_at=time.time(),
            )
        )
        assert len(_lease_files(tmp_path)) == 1
        assert guard.check(_payload("pnpm build")) is None
        holders = [json.loads(p.read_text(encoding="utf-8"))["holder_pid"] for p in _lease_files(tmp_path)]
        assert holders == [CLAUDE.pid]

    def test_back_to_back_launches_reserve_the_first_leases_memory(self, host, tmp_path):
        host.avail = 1400
        assert guard.check(_payload("pnpm build")) is None
        out = guard.check(_payload("pnpm build"))
        assert out is not None and "ram-floor" in _reason(out)
        assert len(_lease_files(tmp_path)) == 1

    def test_unreadable_ram_denies(self, host):
        host.trusted = False
        out = guard.check(_payload("pnpm build"))
        assert out is not None and "unreadable or untrusted" in _reason(out)

    def test_none_reading_denies(self, host, monkeypatch):
        monkeypatch.setattr(guard, "_read_available", lambda: MemoryReading(None, False, "stub"))
        assert guard.check(_payload("pnpm build")) is not None

    @pytest.mark.parametrize("missing", [KEY_FREE_RAM_FLOOR_MB, KEY_LEASE_RESERVE_MB])
    def test_unconfigured_floor_denies_naming_setup(self, host, missing):
        del host.config[missing]
        out = guard.check(_payload("pnpm build"))
        assert "unconfigured" in _reason(out) and "scripts/setup.py" in _reason(out)

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
    def test_malformed_floor_denies(self, host, bad):
        host.config[KEY_FREE_RAM_FLOOR_MB] = bad
        assert guard.check(_payload("pnpm build")) is not None


class TestScopedCarveOut:
    def test_scoped_test_run_skips_the_ram_leg_and_writes_no_lease(self, host, tmp_path):
        host.avail = 1
        out = guard.check(_payload("python -m pytest coordinator_core/bash_guards/tests/test_host_probe.py::test_x"))
        assert out is None
        assert host.ram_reads == 0
        assert _lease_files(tmp_path) == []

    def test_scoped_run_by_a_subagent_is_never_identity_denied(self, host):
        cmd = "python -m pytest coordinator_core/bash_guards/tests/test_host_probe.py"
        assert guard.check(_payload(cmd, agent_id="deadbeef0123", agent_type="coordinator:executor")) is None

    def test_bare_tier_run_stays_heavy(self, host):
        out = guard.check(_payload("python -m pytest", agent_id="deadbeef0123", agent_type="coordinator:executor"))
        assert out is not None and "identity" in _reason(out)


class TestSessionCapLeg:
    def _heavy_rows(self, n):
        return [CLAUDE, CALLER] + [
            ProcRow(pid=300 + i, ppid=CLAUDE.pid, ctime=30 + i, name="node.exe") for i in range(n)
        ]

    def test_at_the_heavy_cap_denies_naming_the_pids(self, host):
        host.rows = self._heavy_rows(2)
        out = guard.check(_payload("pnpm build"))
        text = _reason(out)
        assert "session-cap" in text and "300" in text and "301" in text

    def test_under_the_heavy_cap_admits(self, host):
        host.rows = self._heavy_rows(1)
        assert guard.check(_payload("pnpm build")) is None

    def test_callers_own_wrapper_chain_is_not_counted(self, host):
        host.config[KEY_SESSION_HEAVY_CAP] = "1"
        assert guard.check(_payload("pnpm build")) is None

    def test_background_shells_over_cap_deny_naming_the_pids(self, host):
        host.rows = [CLAUDE, CALLER] + [
            ProcRow(pid=400 + i, ppid=CLAUDE.pid, ctime=40 + i, name="bash.exe") for i in range(3)
        ]
        out = guard.check(_payload("sleep 600", background=True))
        text = _reason(out)
        assert "session-cap" in text and "background" in text and "400" in text

    def test_background_light_command_under_cap_admits(self, host, tmp_path):
        assert guard.check(_payload("sleep 600", background=True)) is None
        assert _lease_files(tmp_path) == []

    def test_foreground_light_command_ignores_shell_count(self, host):
        host.rows = [CLAUDE, CALLER] + [
            ProcRow(pid=400 + i, ppid=CLAUDE.pid, ctime=40 + i, name="bash.exe") for i in range(9)
        ]
        assert guard.check(_payload("ls")) is None

    def test_unreadable_process_table_denies(self, host):
        host.rows = None
        out = guard.check(_payload("pnpm build"))
        assert out is not None and "unreadable" in _reason(out)

    def test_unresolvable_anchor_denies(self, host):
        host.rows = [CALLER]
        out = guard.check(_payload("pnpm build"))
        assert out is not None and "no session anchor" in _reason(out)

    def test_unconfigured_cap_denies(self, host):
        del host.config[KEY_SESSION_HEAVY_CAP]
        out = guard.check(_payload("pnpm build"))
        assert "unconfigured" in _reason(out)

    def test_a_cap_denied_call_writes_no_lease(self, host, tmp_path):
        host.rows = self._heavy_rows(2)
        guard.check(_payload("pnpm build"))
        assert _lease_files(tmp_path) == []

    def test_admitted_call_narrows_an_earlier_unattributed_lease(self, host, tmp_path):
        assert guard.check(_payload("pnpm build")) is None
        host.rows = self._heavy_rows(1)
        host.avail = 9000
        assert guard.check(_payload("pnpm build")) is None
        holders = sorted(json.loads(p.read_text(encoding="utf-8"))["holder_pid"] for p in _lease_files(tmp_path))
        assert holders == [CLAUDE.pid, 300]


class TestOverrideKeys:
    @pytest.fixture()
    def repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        return tmp_path

    def _log(self, repo) -> str:
        logs = list((repo / ".git" / "coordinator-sessions").rglob("overrides.log"))
        return "".join(p.read_text(encoding="utf-8") for p in logs)

    def test_identity_key_bypasses_only_the_identity_leg(self, host, repo):
        env = {OVERRIDE_KEYS["identity"]: "1"}
        host.avail = 900
        out = guard.check(
            _payload("tsc --noEmit", agent_id="d", agent_type="general-purpose", env=env, cwd=repo)
        )
        assert out is not None and "ram-floor" in _reason(out)
        assert OVERRIDE_KEYS["identity"] in self._log(repo)

    def test_ram_key_bypasses_only_the_ram_leg(self, host, repo):
        env = {OVERRIDE_KEYS["ram-floor"]: "1"}
        host.avail = 1
        assert guard.check(_payload("pnpm build", env=env, cwd=repo)) is None
        out = guard.check(
            _payload("pnpm build", agent_id="d", agent_type="general-purpose", env=env, cwd=repo)
        )
        assert out is not None and "identity" in _reason(out)
        assert OVERRIDE_KEYS["ram-floor"] in self._log(repo)

    def test_session_cap_key_bypasses_only_the_cap_leg(self, host, repo):
        env = {OVERRIDE_KEYS["session-cap"]: "1"}
        host.rows = [CLAUDE, CALLER] + [
            ProcRow(pid=300 + i, ppid=CLAUDE.pid, ctime=30 + i, name="node.exe") for i in range(5)
        ]
        assert guard.check(_payload("pnpm build", env=env, cwd=repo)) is None
        host.avail = 1
        assert guard.check(_payload("pnpm build", env=env, cwd=repo)) is not None
        assert OVERRIDE_KEYS["session-cap"] in self._log(repo)

    def test_without_a_key_nothing_is_logged(self, host, repo):
        guard.check(_payload("pnpm build", cwd=repo))
        assert self._log(repo) == ""

    def test_a_key_set_to_anything_but_one_does_not_bypass(self, host, repo):
        env = {OVERRIDE_KEYS["ram-floor"]: "true"}
        host.avail = 1
        assert guard.check(_payload("pnpm build", env=env, cwd=repo)) is not None

    def test_no_payload_field_admits(self, host):
        p = _payload("tsc --noEmit", agent_id="d", agent_type="general-purpose")
        p.update({"justification": "urgent", "priority": "high", "light": True})
        p["tool_input"].update({"justification": "urgent", "light": True})
        assert guard.check(p) is not None


class TestChainIntegration:
    def _eval(self, payload):
        from coordinator_core.bash_guards import dispatch

        out = dispatch.evaluate_payload_json(json.dumps(payload))
        if out is None:
            return "allow", out
        return out.get("hookSpecificOutput", {}).get("permissionDecision"), out

    @pytest.fixture(autouse=True)
    def _isolated_sentinels(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "unlock-home"))
        (tmp_path / "unlock-home").mkdir(exist_ok=True)

    @pytest.fixture()
    def strict(self, monkeypatch):
        """The blocking behaviour the report-only flip restores."""
        from coordinator_core import machine_profile

        real = machine_profile.guard_level
        monkeypatch.setattr(
            machine_profile, "guard_level", lambda name: "strict" if name == GUARD_NAME else real(name)
        )

    def _starved_build(self, session_id, tmp_path, host):
        """An EM main-thread build on a box under its RAM floor: only this guard denies it."""
        host.avail = 1
        return {
            "tool_name": "Bash",
            "session_id": session_id,
            "cwd": str(tmp_path),
            "pid": CALLER.pid,
            "tool_input": {"command": "pnpm build"},
        }

    def test_chain_denies_a_starved_build_naming_the_gate(self, tmp_path, host, strict):
        decision, out = self._eval(self._starved_build("sess-chain-1", tmp_path, host))
        assert decision == "deny"
        assert GUARD_NAME in out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_a_dr260_sentinel_clears_exactly_one_deny(self, tmp_path, host, strict):
        from coordinator_core.session import guard_unlock_sentinel as gus

        sentinel = gus.sentinel_path("sess-chain-2", GUARD_NAME)
        sentinel.write_text("", encoding="utf-8")
        payload = self._starved_build("sess-chain-2", tmp_path, host)
        first, _ = self._eval(payload)
        assert first == "allow" and not sentinel.exists()
        second, out = self._eval(payload)
        assert second == "deny"
        assert GUARD_NAME in out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_report_only_default_allows_silently_and_logs_the_deny(self, tmp_path, host):
        decision, out = self._eval(self._starved_build("sess-chain-3", tmp_path, host))
        assert decision == "allow" and out is None
        assert [line["kind"] for line in _log_lines(tmp_path)] == ["ram-floor"]


class TestDenyTextRegister:
    def _reasons(self, host):
        out = []
        out.append(guard.check(_payload("tsc --noEmit", agent_id="d", agent_type="general-purpose")))
        host.avail = 900
        out.append(guard.check(_payload("pnpm build")))
        host.trusted = False
        out.append(guard.check(_payload("pnpm build")))
        host.trusted = True
        host.avail = 5000
        saved = host.config.pop(KEY_FREE_RAM_FLOOR_MB)
        out.append(guard.check(_payload("pnpm build")))
        host.config[KEY_FREE_RAM_FLOOR_MB] = saved
        host.rows = [CLAUDE, CALLER] + [
            ProcRow(pid=300 + i, ppid=CLAUDE.pid, ctime=30 + i, name="node.exe") for i in range(3)
        ]
        out.append(guard.check(_payload("pnpm build")))
        host.rows = [CLAUDE, CALLER] + [
            ProcRow(pid=400 + i, ppid=CLAUDE.pid, ctime=40 + i, name="bash.exe") for i in range(3)
        ]
        out.append(guard.check(_payload("sleep 9", background=True)))
        host.rows = None
        out.append(guard.check(_payload("pnpm build")))
        return [_reason(o) for o in out]

    def test_every_deny_passes_the_message_register_lint(self, host):
        # The published engine tree omits message_register; the lint runs in the authoring repo.
        lint_message = pytest.importorskip("coordinator_core.message_register.register").lint_message

        reasons = self._reasons(host)
        assert len(reasons) == 7 and all(reasons)
        findings = [f for text in reasons for f in lint_message(text, site=GUARD_NAME)]
        assert findings == []

    def test_no_deny_carries_an_override_key(self, host):
        for text in self._reasons(host):
            assert "COORDINATOR_" not in text


class TestRegistration:
    def test_roster_lists_a_fail_closed_confinement_deny_after_the_suite_guard(self):
        from coordinator_core.bash_guards.roster import guard_roster

        ids = [e.id for e in guard_roster()]
        (entry,) = [e for e in guard_roster() if e.id == GUARD_NAME]
        assert entry.band == "confinement-deny" and entry.fail_closed
        assert ids.index(GUARD_NAME) == ids.index("check-test-suite-invocation") + 1

    def test_module_declares_hard_deny_over_both_command_tools(self):
        assert guard.CLASS == "hard-deny"
        assert set(guard.MATCHERS) == {"Bash", "PowerShell"}


class TestNoThresholdLiterals:
    _MODULES = (
        "guard_heavy_command_admission.py",
        "_heavy_identity.py",
    )

    @pytest.mark.parametrize("name", _MODULES)
    def test_no_numeric_threshold_literal(self, name):
        tree = ast.parse((Path(guard.__file__).parent / name).read_text(encoding="utf-8"))
        exempt = {
            n.value.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_LOG_CMD_CHARS" for t in n.targets)
        }
        offenders = [
            (n.lineno, n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)
            and n.value not in (0, 1, -1)
            and n.lineno not in exempt
        ]
        assert offenders == []

    def test_no_spawn_api_in_the_guard_source(self):
        src = (Path(guard.__file__)).read_text(encoding="utf-8")
        for needle in ("subprocess", "os.system", "os.popen", "posix_spawn", "os.spawn"):
            assert needle not in src


class TestWouldDenyLog:
    def test_every_deny_is_logged_with_leg_class_and_caller(self, host, tmp_path):
        guard.check(_payload("tsc --noEmit", agent_id="a1", agent_type="general-purpose"))
        (line,) = _log_lines(tmp_path)
        assert line["kind"] == "identity" and line["heavy_class"] == "typecheck"
        assert line["agent_type"] == "general-purpose" and line["command"] == "tsc --noEmit"
        assert line["pid_source"] == "payload" and line["caller_pid"] == CALLER.pid

    def test_an_admitted_call_logs_nothing(self, host, tmp_path):
        assert guard.check(_payload("pnpm build")) is None
        assert _log_lines(tmp_path) == []

    def test_a_cap_deny_logs_the_census_names(self, host, tmp_path):
        host.rows = TestSessionCapLeg()._heavy_rows(2)
        guard.check(_payload("pnpm build"))
        (line,) = _log_lines(tmp_path)
        assert line["kind"] == "session-cap" and line["census_heavy"] == ["node.exe", "node.exe"]

    def test_a_missing_payload_pid_is_recorded_as_the_guard_process(self, host, tmp_path):
        payload = _payload("pnpm build")
        del payload["pid"]
        guard.check(payload)
        (line,) = _log_lines(tmp_path)
        assert line["pid_source"] == "guard-process"

    def test_a_crash_is_logged_and_reraised(self, host, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("probe")

        monkeypatch.setattr(guard, "_ram_leg", boom)
        with pytest.raises(RuntimeError):
            guard.check(_payload("pnpm build"))
        (line,) = _log_lines(tmp_path)
        assert line["kind"] == "crash" and "probe" in line["reason"]


class TestRunawayWorkerAdvisory:
    def test_a_worker_over_the_ceiling_is_flagged_and_still_admitted(self, host, tmp_path):
        host.rows = TestSessionCapLeg()._heavy_rows(1)
        host.config[KEY_WORKER_RSS_CEILING_MB] = "4096"
        host.working_sets = {300: 8800}
        assert guard.check(_payload("pnpm build")) is None
        (line,) = _log_lines(tmp_path)
        assert line["kind"] == "worker-rss" and "300" in line["reason"] and "8800" in line["reason"]

    def test_no_ceiling_configured_flags_nothing(self, host, tmp_path):
        host.rows = TestSessionCapLeg()._heavy_rows(1)
        host.working_sets = {300: 8800}
        assert guard.check(_payload("pnpm build")) is None
        assert _log_lines(tmp_path) == []


class TestOneTypecheckPerSession:
    def test_a_second_typecheck_while_one_runs_is_denied(self, host, tmp_path, monkeypatch):
        tsc = ProcRow(pid=300, ppid=CLAUDE.pid, ctime=30, name="node.exe")
        host.rows = [CLAUDE, CALLER, tsc]
        leases.write_lease(
            LeaseRecord(tsc.pid, tsc.ctime, CLAUDE.pid, CLAUDE.ctime, "typecheck", 0.0),
            tmp_path / "leases",
        )
        monkeypatch.setattr(
            guard, "_primitives", lambda: _Prims(host.rows, alive_set={(CLAUDE.pid, CLAUDE.ctime), (tsc.pid, tsc.ctime)})
        )
        text = _reason(guard.check(_payload("tsc --noEmit")))
        assert "already runs a typecheck" in text and "300" in text

    def test_a_build_beside_a_running_typecheck_is_not_denied_for_it(self, host, tmp_path, monkeypatch):
        tsc = ProcRow(pid=300, ppid=CLAUDE.pid, ctime=30, name="node.exe")
        host.rows = [CLAUDE, CALLER, tsc]
        leases.write_lease(
            LeaseRecord(tsc.pid, tsc.ctime, CLAUDE.pid, CLAUDE.ctime, "typecheck", 0.0),
            tmp_path / "leases",
        )
        monkeypatch.setattr(
            guard, "_primitives", lambda: _Prims(host.rows, alive_set={(CLAUDE.pid, CLAUDE.ctime), (tsc.pid, tsc.ctime)})
        )
        assert guard.check(_payload("pnpm build")) is None


class TestVitestWorkerCap:
    def test_capped_vitest_is_admitted_for_a_subagent_without_any_leg(self, host, tmp_path):
        host.config[KEY_VITEST_MAX_WORKERS] = "4"
        out = guard.check(
            _payload("vitest run a.test.ts --maxWorkers=2", agent_id="a1", agent_type="general-purpose", cwd=tmp_path)
        )
        assert out is None

    def test_uncapped_scoped_vitest_from_a_subagent_is_identity_denied(self, host, tmp_path):
        host.config[KEY_VITEST_MAX_WORKERS] = "4"
        out = guard.check(_payload("vitest run a.test.ts", agent_id="a1", agent_type="general-purpose", cwd=tmp_path))
        assert "identity" in _reason(out)


def test_an_unlisted_subagent_never_pays_for_the_workflow_run_lookup(host, monkeypatch):
    def lookup(sid):
        raise AssertionError("workflow-run lookup reached for an unlisted caller")

    monkeypatch.setattr(guard, "_live_workflow_runs", lookup)
    out = guard.check(_payload("tsc --noEmit", agent_id="a1", agent_type="general-purpose"))
    assert "not on the heavy-command allow-list" in _reason(out)
