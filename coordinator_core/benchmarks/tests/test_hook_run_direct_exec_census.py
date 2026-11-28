"""Pure-function pins for the direct-exec census harness: registration rewrite and typed refusals."""

from __future__ import annotations

from coordinator_core.benchmarks import hook_run_direct_exec_census as h
from pathlib import Path
import pytest

LOADER = ["-c", "import os,sys;SEAM", "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/door_launch.py"]
BOOT = "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/_hook_boot.py"
STAGED = "staged-run/hook-run.exe"


def _doc() -> dict:
    return {"hooks": {
        "Stop": [{"hooks": [
            {"type": "command", "command": "python3", "timeout": 5,
             "args": [*LOADER, "--quiet", "--pass-2", "hooks.guard_turn_intent", BOOT]},
        ]}],
        "SessionEnd": [{"hooks": [
            {"type": "command", "command": "python3", "timeout": 15,
             "args": [*LOADER, "--quiet", "--pass-2", "--advisory", "hooks.sessionend_archive_session", BOOT]},
        ]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/x/bin/hook-run hooks.preuse_bash_dispatch", "timeout": 10},
            {"type": "command", "command": "python3", "args": ["-c", "print(1)", "other.py"]},
        ]}],
    }}


def test_door_registrations_become_direct_exec_and_others_stay() -> None:
    out, count = h.rewrite_hooks(_doc(), STAGED)
    assert count == 3
    stop = out["hooks"]["Stop"][0]["hooks"][0]
    assert stop == {"type": "command", "timeout": 5, "command": STAGED,
                    "args": ["--stdin-hook", "--advisory", "hooks.guard_turn_intent"]}
    end = out["hooks"]["SessionEnd"][0]["hooks"][0]
    assert end["args"] == ["--stdin-hook", "--advisory", "hooks.sessionend_archive_session"]
    shell, other = out["hooks"]["PreToolUse"][0]["hooks"]
    assert shell["command"] == STAGED and shell["args"] == ["--stdin-hook", "hooks.preuse_bash_dispatch"]
    assert other == {"type": "command", "command": "python3", "args": ["-c", "print(1)", "other.py"]}


def test_rewrite_leaves_the_source_document_untouched() -> None:
    doc = _doc()
    before = repr(doc)
    h.rewrite_hooks(doc, STAGED)
    assert repr(doc) == before


def test_non_door_document_rewrites_nothing() -> None:
    doc = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3", "args": ["x.py"]}]}]}}
    out, count = h.rewrite_hooks(doc, STAGED)
    assert count == 0 and out == doc


def test_missing_census_is_a_typed_refusal_naming_the_flag(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.json"
    hooks.write_text("{}", encoding="utf-8")
    for kwargs, flag in (({"census_path": None, "hooks_json": hooks}, "--census"),
                         ({"census_path": hooks, "hooks_json": None}, "--hooks-json"),
                         ({"census_path": tmp_path / "nope.py", "hooks_json": hooks}, "--census")):
        try:
            h.run(image=None, fires=1, out=None, **kwargs)
        except h.CensusRefusal as exc:
            assert flag in str(exc)
        else:
            raise AssertionError(f"{flag} refusal expected")


def test_main_maps_refusal_to_exit_2(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "declare_benchmark_origin", lambda: None)
    monkeypatch.setattr(h, "default_doe_paths", lambda: (None, None))
    assert h.main([]) == 2
    assert "--census" in capsys.readouterr().err


def test_main_defaults_census_and_hooks_to_the_registered_doe_repo(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    seen: dict = {}
    census, hooks = tmp_path / "c.py", tmp_path / "hooks.json"
    monkeypatch.setattr(h, "declare_benchmark_origin", lambda: None)
    monkeypatch.setattr(h, "default_doe_paths", lambda: (census, hooks))

    def fake_run(census_path, hooks_json, image, fires, out, rewrite, with_unrewritten):
        seen.update(census=census_path, hooks=hooks_json, rewrite=rewrite, unrewritten=with_unrewritten)
        return {"per_fire_total": {}, "runs": [], "warm": {"scope": "none", "idle_write_bytes_per_s": None,
                "inventory": {"elected_pids": [], "orphan_pids": [], "duplicate_roots": []}}}

    monkeypatch.setattr(h, "run", fake_run)
    assert h.main([]) == 0
    assert seen == {"census": census, "hooks": hooks, "rewrite": True, "unrewritten": True}
    assert h.main(["--rewritten-only"]) == 0 and seen["unrewritten"] is False


def test_server_engine_root_reads_the_clone_from_the_server_script_arg(tmp_path: Path) -> None:
    clone = tmp_path / "klabauter-clone"
    cmd = ["python3", str(clone / "coordinator_core" / "warm" / "server.py")]
    assert h.server_engine_root(cmd) == clone
    assert h.server_engine_root(["python", "-m", "pytest", "coordinator_core/warm/tests"]) is None
    assert h.server_engine_root([]) is None


def test_inventory_names_the_elected_the_duplicates_and_the_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = [{"pid": 1, "parent_alive": True, "engine_root": "R"},
               {"pid": 2, "parent_alive": False, "engine_root": "R"},
               {"pid": 3, "parent_alive": True, "engine_root": "S"}]
    monkeypatch.setattr(h, "breadcrumb_pid", lambda root: 1 if str(root) == "R" else None)
    inv = h.warm_inventory(servers)
    assert inv["elected_pids"] == [1]
    assert inv["duplicate_roots"] == ["R"] and inv["orphan_pids"] == [2]


def test_changed_files_names_created_and_grown_files_only(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "steady.json").write_text("x", encoding="utf-8")
    (tmp_path / "telemetry.jsonl").write_text("ab", encoding="utf-8")
    before = h.snapshot_tree(tmp_path)
    (tmp_path / "telemetry.jsonl").write_text("abcdef", encoding="utf-8")
    (tmp_path / "sub" / "new.log").write_text("zz", encoding="utf-8")
    rows = h.changed_files(before, h.snapshot_tree(tmp_path))
    assert [(r["file"], r["size_delta"], r["created"]) for r in rows] == [
        ("sub/new.log", 2, True), ("telemetry.jsonl", 4, False)]


def test_per_request_files_collapse_into_one_family() -> None:
    rows = [{"file": "ack-journal/d20261124/9712-6178-77.outcome.json", "size_delta": 147},
            {"file": "ack-journal/d20261124/9712-6179-88.outcome.json", "size_delta": 273},
            {"file": "request-telemetry.jsonl", "size_delta": 40}]
    assert h.collapse_files(rows) == {
        "ack-journal/dN/N-N-N.outcome.json": {"files": 2, "size_delta": 420},
        "request-telemetry.jsonl": {"files": 1, "size_delta": 40}}


def _row(op: str, event: str, matcher: str, **kw) -> dict:
    base = {"op": op, "event": event, "form": "exec", "bash_tree": 0.0, "conhost_tree": 0.0,
            "python_tree": 1.0, "hook-run_tree": 0.0, "total_tree": 1.0, "rc_set": [0]}
    return {**base, **kw}


def test_attach_fires_means_per_door_and_nets_out_the_idle_rate() -> None:
    rows = [_row("hooks.a", "PreToolUse", ""), _row("hooks.b", "Stop", "")]
    doors = [{"matcher": "Bash"}, {"matcher": ""}]
    recs = [{"cpu_s": 0.2, "wall_s": 1.0, "warm_write_bytes": 1000, "warm_files": [{"file": "t.jsonl"}]},
            {"cpu_s": 0.4, "wall_s": 1.0, "warm_write_bytes": 500, "warm_files": []},
            {"cpu_s": 0.1, "wall_s": 1.0, "warm_write_bytes": None, "warm_files": []},
            {"cpu_s": 0.3, "wall_s": 1.0, "warm_write_bytes": None, "warm_files": []}]
    out = h.attach_fires(rows, doors, recs, fires=2, idle_rate=100.0)
    assert out[0]["matcher"] == "Bash" and out[0]["cpu_s"] == pytest.approx(0.3)
    assert out[0]["warm_write_bytes"] == 750 and out[0]["warm_write_bytes_net"] == 650
    assert out[0]["warm_files"] == {"t.jsonl": 1}
    assert out[1]["warm_write_bytes"] is None


def test_group_per_event_sums_registrations_and_splits_by_matcher() -> None:
    rows = [_row("hooks.a", "PreToolUse", "Bash", total_tree=2.0, cpu_s=0.5, warm_write_bytes=100.0,
                 warm_write_bytes_net=80.0, warm_files={"t.jsonl": 5}),
            _row("hooks.b", "PreToolUse", "Edit", total_tree=1.0, cpu_s=0.25, warm_write_bytes=50.0,
                 warm_write_bytes_net=40.0, warm_files={"t.jsonl": 5, "r.jsonl": 5}),
            _row("hooks.c", "Stop", "", cpu_s=0.0)]
    per = h.group_per_event(rows)
    pre = per["PreToolUse"]
    assert pre["registrations"] == 2 and pre["total_tree"] == 3.0 and pre["python_tree"] == 2.0
    assert pre["warm_write_bytes"] == 150.0 and pre["warm_write_bytes_net"] == 120.0
    assert pre["warm_files"] == {"r.jsonl": 5, "t.jsonl": 10}
    assert pre["by_matcher"]["Bash"]["warm_write_bytes"] == 100.0 and set(pre["by_matcher"]) == {"Bash", "Edit"}
    assert per["Stop"]["warm_write_bytes"] is None
    assert "PreToolUse" in h.render_per_event("x", per)


def test_instrumented_fire_records_cpu_bytes_and_files_for_a_real_child(tmp_path: Path) -> None:
    import subprocess
    import sys
    from types import SimpleNamespace

    class Probe:
        def __init__(self) -> None:
            self.n = 0

        def write_bytes(self) -> int:
            self.n += 1
            return 1000 * self.n

        def snapshot(self) -> dict:
            return {}

        def files_between(self, before: dict, after: dict) -> list:
            return [{"file": "request-telemetry.jsonl", "size_delta": 7, "created": False}]

    census = SimpleNamespace(
        _NO_CONSOLE=getattr(subprocess, "CREATE_NO_WINDOW", 0), _TAIL_S=0.0, _POLL_S=0.005,
        NAMES=("bash", "conhost", "python", "hook-run"), BENIGN_EVENT={"hook_event_name": "PreToolUse"},
        _family=lambda name: "python" if name.lower().startswith("python") else None)
    records: list = []
    h.instrument_fire(census, Probe(), records)
    result = census.fire_once([sys.executable, "-c", "import sys; sys.stdin.read()"], {}, str(tmp_path))
    assert result["rc"] == 0 and result["tree"]["total"] >= 1
    assert len(records) == 1 and records[0]["warm_write_bytes"] == 1000
    assert records[0]["warm_files"][0]["file"] == "request-telemetry.jsonl"
    assert records[0]["cpu_s"] >= 0.0 and records[0]["wall_s"] > 0.0


def test_stage_image_names_it_hook_run_and_carries_the_sidecar(tmp_path: Path) -> None:
    image = tmp_path / "door-w.exe"
    image.write_bytes(b"MZ")
    (tmp_path / "door.engine-root.txt").write_text("E:/engine\n", encoding="utf-8")
    staged = h.stage_image(image, tmp_path / "run")
    assert staged.name == h.staged_name() and staged.read_bytes() == b"MZ"
    assert (tmp_path / "run" / "door.engine-root.txt").read_text(encoding="utf-8") == "E:/engine\n"
