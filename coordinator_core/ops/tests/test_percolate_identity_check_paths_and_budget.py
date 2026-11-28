"""Changed-paths scan view and process-time bound of percolate.run_identity_check.

Fixtures are synthetic tmp_path trees with a fake checker that lists the files it
can see; no persona names, no live codenames.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import external_tool_budget
from coordinator_core.ops import percolate_identity_check as mod

_SCRIPT_REL = ".github/scripts/check-persona-names.py"
_LISTING_CHECKER = (
    "import os\n"
    "print('CWD=' + os.path.basename(os.getcwd()))\n"
    "for root, _dirs, files in os.walk('.'):\n"
    "    for name in files:\n"
    "        print('F=' + os.path.relpath(os.path.join(root, name), '.').replace(os.sep, '/'))\n"
)


@pytest.fixture
def dest(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "dest"
    for rel, body in {
        _SCRIPT_REL: _LISTING_CHECKER,
        ".github/scripts/_repo.py": "",
        ".identity-allowlist": "",
        "a.txt": "a",
        "sub/b.txt": "b",
        "c.txt": "c",
    }.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    monkeypatch.setattr(mod, "resolve_python_interp", lambda: sys.executable)
    return root


def _listed(findings: str) -> list:
    return sorted(line[2:] for line in findings.splitlines() if line.startswith("F="))


class TestScanView:
    def test_view_holds_exactly_the_passed_paths(self, dest: Path) -> None:
        result = mod.run_identity_check(
            str(dest), paths=["a.txt", "sub/b.txt", _SCRIPT_REL, ".github/scripts/_repo.py"]
        )

        assert result["mode"] == "changed"
        assert _listed(result["findings"]) == sorted(
            ["a.txt", "sub/b.txt", _SCRIPT_REL, ".github/scripts/_repo.py"]
        )
        assert result["scanned"] == 4
        assert result["exit_code"] == 0

    def test_view_is_deleted_after_the_run(self, dest: Path) -> None:
        mod.run_identity_check(str(dest), paths=["a.txt", _SCRIPT_REL])

        assert [p.name for p in dest.parent.iterdir()] == ["dest"]

    def test_view_is_deleted_when_the_run_faults(self, dest: Path, monkeypatch) -> None:
        def _boom(*_a, **_k):
            raise RuntimeError("budget")

        monkeypatch.setattr(mod, "run_checker_with_process_budget", _boom)
        with pytest.raises(RuntimeError, match="budget"):
            mod.run_identity_check(str(dest), paths=["a.txt"])

        assert [p.name for p in dest.parent.iterdir()] == ["dest"]

    def test_checker_script_is_added_when_the_caller_omits_it(self, dest: Path) -> None:
        result = mod.run_identity_check(str(dest), paths=["a.txt"])

        assert _listed(result["findings"]) == sorted(["a.txt", _SCRIPT_REL])

    def test_deleted_path_is_skipped_not_fatal(self, dest: Path) -> None:
        result = mod.run_identity_check(str(dest), paths=["gone.txt", "a.txt", _SCRIPT_REL])

        assert _listed(result["findings"]) == sorted(["a.txt", _SCRIPT_REL])
        assert result["scanned"] == 2

    def test_hardlink_refusal_falls_back_to_copy(self, dest: Path, monkeypatch) -> None:
        def _refuse(*_a, **_k):
            raise OSError("cross-device")

        monkeypatch.setattr(mod.os, "link", _refuse)
        result = mod.run_identity_check(str(dest), paths=["a.txt", _SCRIPT_REL])

        assert _listed(result["findings"]) == sorted(["a.txt", _SCRIPT_REL])

    @pytest.mark.parametrize("bad", ["../escape.txt", "/abs.txt", "a/../../x", "x:rel.txt", "sub/y:z.txt"])
    def test_non_relative_path_is_refused(self, dest: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            mod.run_identity_check(str(dest), paths=[bad])

    def test_symlink_source_is_copied_not_linked(self, dest: Path) -> None:
        target = dest / "a.txt"
        link = dest / "ln.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")

        view, count = mod._build_scan_view(dest, ["ln.txt"])
        try:
            assert count == 1
            assert not (view / "ln.txt").is_symlink()
            assert (view / "ln.txt").read_text(encoding="utf-8") == "a"
        finally:
            import shutil

            shutil.rmtree(view, ignore_errors=True)

    def test_empty_paths_spawn_nothing(self, dest: Path, monkeypatch) -> None:
        def _no_spawn(*_a, **_k):
            raise AssertionError("an empty changed set must not spawn")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        result = mod.run_identity_check(str(dest), paths=[])

        assert result == {
            "ran": False,
            "skipped": False,
            "exit_code": 0,
            "findings": "",
            "scanned": 0,
            "mode": "changed",
        }

    def test_no_paths_is_the_full_sweep_over_dest(self, dest: Path) -> None:
        result = mod.run_identity_check(str(dest))

        assert result["mode"] == "full"
        assert "CWD=dest" in result["findings"]
        assert "F=c.txt" in result["findings"]

    def test_full_sweep_reports_the_checker_scanned_count(self, dest: Path) -> None:
        (dest / _SCRIPT_REL).write_text(
            "print('Identity check passed (7 text files scanned, 7 paths checked).')\n",
            encoding="utf-8",
        )

        assert mod.run_identity_check(str(dest))["scanned"] == 7


class TestProcessBudget:
    def _fake(self, readings, done_after=None):
        state = {"i": 0, "terminated": False, "now": 0.0}

        def read():
            i = min(state["i"], len(readings) - 1)
            state["i"] += 1
            return readings[i]

        def poll_done():
            if done_after is not None and state["i"] >= done_after:
                return 0, readings[-1]
            return None

        def terminate():
            state["terminated"] = True

        def clock():
            return state["now"]

        def sleep(seconds):
            state["now"] += seconds

        return state, dict(
            poll_done=poll_done,
            read_process_s=read,
            terminate=terminate,
            clock=clock,
            sleep=sleep,
            poll_s=1.0,
            wall_cap_s=1000.0,
        )

    def test_breach_raises_naming_process_seconds_and_terminates(self) -> None:
        state, kw = self._fake([1.0, 20.0, 41.5])

        with pytest.raises(RuntimeError, match=r"41\.5 process seconds"):
            mod._supervise(budget_s=40.0, no_progress_s=100.0, **kw)

        assert state["terminated"] is True

    def test_under_budget_exit_returns_rc_and_seconds(self) -> None:
        state, kw = self._fake([1.0, 2.0, 3.0], done_after=3)

        assert mod._supervise(budget_s=40.0, no_progress_s=100.0, **kw) == (0, 3.0)
        assert state["terminated"] is False

    def test_stalled_cpu_is_a_hang_fault(self) -> None:
        state, kw = self._fake([5.0])

        with pytest.raises(RuntimeError, match="stopped advancing"):
            mod._supervise(budget_s=40.0, no_progress_s=3.0, **kw)

        assert state["terminated"] is True

    def test_unreadable_cpu_is_backstopped_by_wall_cap(self) -> None:
        state, kw = self._fake([None])
        kw["wall_cap_s"] = 5.0

        with pytest.raises(RuntimeError, match="hang backstop"):
            mod._supervise(budget_s=40.0, no_progress_s=3.0, **kw)

        assert state["terminated"] is True

    def test_real_child_over_budget_is_killed_and_faults(self, tmp_path: Path) -> None:
        busy = tmp_path / "busy.py"
        busy.write_text(
            "import time\nend = time.time() + 30\nwhile time.time() < end:\n    pass\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="process seconds"):
            mod.run_checker_with_process_budget(
                [sys.executable, str(busy)], cwd=tmp_path, env=dict(os.environ), budget_s=0.5
            )

    def test_real_child_within_budget_returns_output(self, tmp_path: Path) -> None:
        quick = tmp_path / "quick.py"
        quick.write_text("import sys\nprint('ok')\nsys.exit(3)\n", encoding="utf-8")

        run = mod.run_checker_with_process_budget(
            [sys.executable, str(quick)], cwd=tmp_path, env=dict(os.environ), budget_s=30.0
        )

        assert run.returncode == 3
        assert run.stdout.strip() == "ok"
        assert run.process_s >= 0.0

    def test_spawn_has_no_wall_timeout_and_no_console_window(
        self, dest: Path, monkeypatch
    ) -> None:
        real_popen = subprocess.Popen
        calls = []

        def spy(*args, **kwargs):
            calls.append(kwargs)
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", spy)
        mod.run_identity_check(str(dest), paths=["a.txt", _SCRIPT_REL])

        assert len(calls) == 1
        assert "timeout" not in calls[0]
        if sys.platform == "win32":
            assert calls[0]["creationflags"] & 0x08000000
        else:
            assert calls[0]["stdin"] is subprocess.DEVNULL

    def test_site_row_records_a_process_bound(self) -> None:
        site = external_tool_budget.EXTERNAL_TOOL_SITES[mod._IDENTITY_SITE]

        assert site.bound_secs == 40.0
        assert mod._PROCESS_BUDGET_S == 40.0


class TestHandler:
    def test_non_list_paths_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="paths"):
            asyncio.run(mod._percolate_run_identity_check({"dest": str(tmp_path), "paths": "a.txt"}))

    def test_paths_reach_the_op(self, dest: Path) -> None:
        result = asyncio.run(
            mod._percolate_run_identity_check({"dest": str(dest), "paths": ["a.txt", _SCRIPT_REL]})
        )

        assert result["mode"] == "changed"
        assert _listed(result["findings"]) == sorted(["a.txt", _SCRIPT_REL])
