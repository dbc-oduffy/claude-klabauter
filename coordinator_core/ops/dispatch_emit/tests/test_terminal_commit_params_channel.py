"""Unit pins for the terminal-commit params channel contract."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as ch
from coordinator_core.win_portability import no_console_creationflags

PARAMS = {
    "run_id": "warp-x-1",
    "run_token": "a" * 32,
    "script_path": "D:\\w\\run.js",  # abs-path-ok: fixture backslash path
    "paths": ["C:\\x", "E:\\y\\z"],
    "note": 'say "hi"\nsecond line',
}
HEADER = "[Workflow harness \u2014 computed task] The task text below was computed at runtime.\n"


def _indent(text: str) -> str:
    return HEADER + "\n".join("  " + ln for ln in text.split("\n"))


def _spawn_file(tmp_path, first_record, extra=""):
    p = tmp_path / "proj" / "subagents" / "workflows" / "wf_x" / "agent-a.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(first_record) + "\n" + extra, encoding="utf-8")
    return p


def _record(prompt):
    return {"type": "user", "message": {"role": "user", "content": prompt}}


@pytest.mark.parametrize("root", ["C:/Users/x/repo", "/home/x/repo"])
def test_params_path_is_forward_slash_absolute_shape(root):
    p = ch.params_path(root, "warp-x-1")
    assert p is not None
    assert "\\" not in p.as_posix()
    assert p.as_posix().endswith("/scratch/warp/warp-x-1/terminal-commit-params.json")


@pytest.mark.parametrize("bad", ["", "../x", "a/b", None, 5, "-x"])
def test_params_path_rejects_bad_run_id(bad):
    assert ch.params_path("/r", bad) is None


def test_block_round_trips_with_backslashes_quotes_and_newlines():
    assert ch.prompt_params(ch.render_params_block(PARAMS)) == PARAMS


def test_harness_indented_copy_parses_the_same():
    prompt = "run this\n" + ch.render_params_block(PARAMS) + "\nafter"
    assert ch.prompt_params(_indent(prompt)) == PARAMS


def test_block_survives_line_separator_characters_in_a_value():
    params = {**PARAMS, "note": "a b c\u0085d"}
    assert ch.prompt_params(_indent("x\n" + ch.render_params_block(params))) == params


def test_prompt_params_rejects_malformed():
    block = ch.render_params_block(PARAMS)
    no_token = {k: v for k, v in PARAMS.items() if k != "run_token"}
    for bad in (
        "no block here",
        block + "\n" + block,
        f"{ch.BLOCK_OPEN}\n[1]\n{ch.BLOCK_CLOSE}",
        ch.render_params_block(no_token),
        f"{ch.BLOCK_OPEN}\nnot json\n{ch.BLOCK_CLOSE}",
        None,
    ):
        assert ch.prompt_params(bad) is None


def test_read_spawn_prompt_returns_prompt_exactly(tmp_path):
    prompt = _indent("x\n" + ch.render_params_block(PARAMS))
    assert ch.read_spawn_prompt(str(_spawn_file(tmp_path, _record(prompt)))) == prompt


def test_read_spawn_prompt_accepts_backslash_separators(tmp_path, monkeypatch):
    f = _spawn_file(tmp_path, _record("hello"))
    winpath = str(f).replace("/", "\\")
    real_open = open

    def open_translating(path, *args, **kwargs):
        # POSIX treats backslashes as filename characters; map back so the Windows-separator
        # path still reaches the real file.
        return real_open(str(path).replace("\\", "/"), *args, **kwargs)

    monkeypatch.setattr(ch, "open", open_translating, raising=False)
    assert "\\" in winpath and "/" not in winpath
    assert ch.read_spawn_prompt(winpath) == "hello"


def test_read_spawn_prompt_requires_workflow_spawn_segments(tmp_path):
    f = tmp_path / "plain.jsonl"
    f.write_text(json.dumps(_record("p")) + "\n", encoding="utf-8")
    assert ch.read_spawn_prompt(str(f)) is None
    g = tmp_path / "subagents" / "agent-a.jsonl"
    g.parent.mkdir()
    g.write_text(json.dumps(_record("p")) + "\n", encoding="utf-8")
    assert ch.read_spawn_prompt(str(g)) is None


def test_read_spawn_prompt_rejects_unreadable_shapes(tmp_path):
    assert ch.read_spawn_prompt(str(tmp_path / "subagents" / "workflows" / "m.jsonl")) is None
    assert ch.read_spawn_prompt(None) is None
    d = tmp_path / "subagents" / "workflows" / "wf"
    d.mkdir(parents=True)
    bad = d / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    assert ch.read_spawn_prompt(str(bad)) is None
    big = d / "big.jsonl"
    big.write_text(json.dumps(_record("x" * (ch.READ_CAP_BYTES + 10))) + "\n", encoding="utf-8")
    assert ch.read_spawn_prompt(str(big)) is None
    nonstr = d / "nonstr.jsonl"
    nonstr.write_text(json.dumps(_record([{"type": "text"}])) + "\n", encoding="utf-8")
    assert ch.read_spawn_prompt(str(nonstr)) is None
    notuser = d / "notuser.jsonl"
    notuser.write_text(json.dumps({"type": "assistant", "message": {"content": "p"}}) + "\n")
    assert ch.read_spawn_prompt(str(notuser)) is None


RELAY = f"{ch.RELAY_HEADER} please run the thing"


def _lines(*records):
    return "".join(json.dumps(r) + "\n" for r in records)


def test_read_spawn_prompt_skips_non_user_records_and_returns_first_non_relay_user_record(tmp_path):
    f = _spawn_file(tmp_path, {"type": "system"}, extra=_lines(_record("second")))
    assert ch.read_spawn_prompt(str(f)) == "second"
    g = _spawn_file(tmp_path / "g", _record("first"), extra=_lines(_record("second")))
    assert ch.read_spawn_prompt(str(g)) == "first"


def test_read_spawn_prompt_returns_computed_record_behind_the_relay(tmp_path):
    prompt = _indent("x\n" + ch.render_params_block(PARAMS))
    f = _spawn_file(tmp_path, _record(RELAY), extra=_lines(_record(prompt)))
    assert ch.read_spawn_prompt(str(f)) == prompt
    assert ch.prompt_params(ch.read_spawn_prompt(str(f))) == PARAMS


def test_block_inside_the_relay_record_is_never_returned(tmp_path):
    relay = RELAY + "\n" + ch.render_params_block(PARAMS)
    f = _spawn_file(tmp_path, _record(relay), extra=_lines(_record("no block")))
    assert ch.prompt_params(ch.read_spawn_prompt(str(f))) is None


def test_relay_with_over_cap_second_record_returns_none(tmp_path):
    f = _spawn_file(tmp_path, _record(RELAY), extra=_lines(_record("x" * (ch.READ_CAP_BYTES + 10))))
    assert ch.read_spawn_prompt(str(f)) is None


def test_third_record_is_never_read(tmp_path):
    block = _indent(ch.render_params_block(PARAMS))
    f = _spawn_file(tmp_path, _record(RELAY), extra=_lines(_record("second"), _record(block)))
    assert ch.read_spawn_prompt(str(f)) == "second"


def test_prompt_record_beyond_max_prompt_records_is_never_read(tmp_path):
    filler = [{"type": "attachment"}] * ch.MAX_PROMPT_RECORDS
    f = _spawn_file(tmp_path, _record(RELAY), extra=_lines(*filler, _record("late")))
    assert ch.read_spawn_prompt(str(f)) is None


def test_materialize_writes_ascii_json_equal_to_input_and_replaces(tmp_path):
    target = tmp_path / "scratch" / "warp" / "r" / ch.PARAMS_FILE_NAME
    assert ch.materialize(target, {"old": "\u00e9"})
    params = {**PARAMS, "uni": "caf\u00e9 \u2014"}
    assert ch.materialize(target, params)
    raw = target.read_bytes()
    raw.decode("ascii")
    assert json.loads(raw) == params
    assert [p.name for p in target.parent.iterdir()] == [ch.PARAMS_FILE_NAME]


def test_materialize_returns_false_when_parent_is_a_file(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    assert ch.materialize(blocker / "sub" / ch.PARAMS_FILE_NAME, PARAMS) is False


def test_missing_file_diagnosis_names_run_and_writer(tmp_path):
    diagnosis = ch.missing_file_diagnosis(str(tmp_path / "scratch" / "warp" / "run-1" / ch.PARAMS_FILE_NAME))
    assert diagnosis is not None
    assert "run-1" in diagnosis
    assert "block_subagent_commit" in diagnosis
    assert "Not dispatched" in diagnosis


def test_missing_file_diagnosis_accepts_forward_slash_and_relative():
    assert ch.missing_file_diagnosis("scratch/warp/run-1/" + ch.PARAMS_FILE_NAME) is not None


@pytest.mark.skipif(sys.platform != "win32", reason="PurePosixPath does not split on backslash")
def test_missing_file_diagnosis_accepts_backslash_separators():
    assert ch.missing_file_diagnosis("r\\scratch\\warp\\run-1\\" + ch.PARAMS_FILE_NAME) is not None


@pytest.mark.parametrize(
    "path",
    [
        "scratch/warp/run-1/other.json",
        "scratch/warp/-bad/" + ch.PARAMS_FILE_NAME,
        "scratch/other/run-1/" + ch.PARAMS_FILE_NAME,
        "warp/run-1/" + ch.PARAMS_FILE_NAME,
        ch.PARAMS_FILE_NAME,
        "",
    ],
)
def test_missing_file_diagnosis_refuses_other_paths(path):
    assert ch.missing_file_diagnosis(path) is None


@pytest.mark.spawns_process
def test_js_and_python_block_agree():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    prog = ch.PARAMS_BLOCK_JS + f"\nprocess.stdout.write(_terminalCommitParamsBlock({json.dumps(PARAMS)}));"
    out = subprocess.run(
        [node, "-"], input=prog.encode("utf-8"), capture_output=True, timeout=30, **no_console_creationflags()
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.decode("utf-8") == ch.render_params_block(PARAMS)
