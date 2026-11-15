"""Pure-function pins for the direct-exec census harness: registration rewrite and typed refusals."""

from __future__ import annotations

from coordinator_core.benchmarks import hook_run_direct_exec_census as h

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


def test_door_registrations_become_direct_exec_and_others_stay():
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


def test_rewrite_leaves_the_source_document_untouched():
    doc = _doc()
    before = repr(doc)
    h.rewrite_hooks(doc, STAGED)
    assert repr(doc) == before


def test_non_door_document_rewrites_nothing():
    doc = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3", "args": ["x.py"]}]}]}}
    out, count = h.rewrite_hooks(doc, STAGED)
    assert count == 0 and out == doc


def test_missing_census_is_a_typed_refusal_naming_the_flag(tmp_path):
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


def test_main_maps_refusal_to_exit_2(capsys, monkeypatch):
    monkeypatch.setattr(h, "declare_benchmark_origin", lambda: None)
    assert h.main([]) == 2
    assert "--census" in capsys.readouterr().err


def test_stage_image_names_it_hook_run_and_carries_the_sidecar(tmp_path):
    image = tmp_path / "door-w.exe"
    image.write_bytes(b"MZ")
    (tmp_path / "door.engine-root.txt").write_text("E:/engine\n", encoding="utf-8")
    staged = h.stage_image(image, tmp_path / "run")
    assert staged.name == h.staged_name() and staged.read_bytes() == b"MZ"
    assert (tmp_path / "run" / "door.engine-root.txt").read_text(encoding="utf-8") == "E:/engine\n"
