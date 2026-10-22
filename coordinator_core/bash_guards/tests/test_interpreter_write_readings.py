"""cwd-drive readings of `/x/...` literals and node write-shape extraction."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _write_bump_sink_shapes as sinks

_CWD_LETTER = chr(ord("A") + 5)
_OTHER = chr(ord("A") + 4).lower()


def _cwd(tmp_path):
    return f"{_CWD_LETTER}:\\{tmp_path.name}"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)


def test_other_drive_gives_two_readings(windows, tmp_path):
    out = sinks.interpreter_write_target_readings(f"/{_OTHER}/x/y", _cwd(tmp_path))
    assert out == [f"/{_OTHER}/x/y", f"{_CWD_LETTER}:\\{_OTHER}\\x\\y"]


def test_same_drive_gives_one_reading(windows, tmp_path):
    raw = f"/{_CWD_LETTER.lower()}/x/y"
    assert sinks.interpreter_write_target_readings(raw, _cwd(tmp_path)) == [raw]
    assert sinks.cwd_drive_reading(raw, _cwd(tmp_path)) is None


def test_posix_host_gives_one_reading(monkeypatch, tmp_path):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: False)
    raw = f"/{_OTHER}/x/y"
    assert sinks.interpreter_write_target_readings(raw, str(tmp_path)) == [raw]


def test_never_raises_on_odd_input(windows):
    assert sinks.cwd_drive_reading(None, None) is None
    assert sinks.cwd_drive_reading("relative/x", f"{_CWD_LETTER}:\\a") is None
    assert sinks.cwd_drive_reading("/e/x", None) is None


NODE_SHAPES = [
    "fs.writeFileSync('/o/a.txt', 'x')",
    "fs.appendFileSync(\"/o/a.txt\", 'x')",
    "fs.mkdirSync('/o/a.txt', {recursive: true})",
    "fs.writeFile('/o/a.txt', 'x', cb)",
    "fs.promises.writeFile('/o/a.txt', 'x')",
    "fs.promises.mkdir('/o/a.txt')",
    "fs.promises.appendFile('/o/a.txt', 'x')",
    "fs.copyFileSync(src, '/o/a.txt')",
    "fs.renameSync('a', '/o/a.txt')",
]


@pytest.mark.parametrize("shape", NODE_SHAPES)
def test_node_shape_in_heredoc(shape):
    cmd = f"node - <<'EOF'\n{shape}\nEOF"
    assert sinks.extract_interpreter_payload_write_sink_targets(cmd) == ["/o/a.txt"]


@pytest.mark.parametrize("shape", NODE_SHAPES)
def test_node_shape_in_eval(shape):
    cmd = f'node -e "{shape.replace(chr(34), chr(39))}"'
    assert sinks.extract_interpreter_payload_write_sink_targets(cmd) == ["/o/a.txt"]


def test_node_long_eval_flag():
    cmd = "node --eval \"fs.writeFileSync('/o/a.txt','x')\""
    assert sinks.extract_interpreter_payload_write_sink_targets(cmd) == ["/o/a.txt"]


def test_template_literal_and_variable_yield_nothing():
    text = "fs.writeFileSync(`/o/${x}.txt`, 'a'); fs.writeFileSync(p, 'a'); fs.copyFileSync(a, b)"
    assert sinks._node_write_targets_in_text(text) == []


def test_comments_are_stripped():
    text = "// fs.writeFileSync('/o/a.txt','x')\n/* fs.mkdirSync('/o/b') */\n"
    assert sinks._node_write_targets_in_text(text) == []
