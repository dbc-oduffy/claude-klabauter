"""The terminal_commit command line holds no escaped backslash pair a retype or unquoted heredoc can halve."""

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_token, sibling_split, wake_digest
from coordinator_core.win_portability import no_console_creationflags

PARAMS = {
    "run_id": "r1",
    "run_token": "tok",
    "script_path": r"C:\work\.coordinator-local\y.md",  # abs-path-ok: escape fixture
    "note": 'quote " and \\n literal and newline\n',
}


def _body(cli: str) -> str:
    return cli.split("<<'JSON'\n", 1)[1].rsplit("\nJSON", 1)[0]


SIDECAR_PARAMS = {
    "run_id": "r1",
    "run_token": "tok",
    "script_path": r"D:\scratch\run\y.mjs",  # abs-path-ok: escape fixture
    "wave_sidecar_paths": [r"E:\x\.coordinator-local\s.md"],  # abs-path-ok: escape fixture
}


@pytest.mark.parametrize("params", [PARAMS, SIDECAR_PARAMS])
def test_python_cli_survives_unquoted_heredoc_and_retype(params):
    body = _body(sibling_split._cli(params))
    assert "\\\\" not in body
    assert "\\u005c" in body
    assert json.loads(body) == params


def test_token_params_still_parse():
    got = run_token.token_params_from_command(sibling_split._cli(PARAMS))
    assert got["script_path"] == PARAMS["script_path"]


@pytest.mark.spawns_process
@pytest.mark.parametrize("params", [PARAMS, SIDECAR_PARAMS])
def test_js_and_python_helpers_agree(params):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    prog = (
        wake_digest.TERMINAL_COMMIT_CLI_HELPER_JS
        + f"\nprocess.stdout.write(_terminalCommitCli({json.dumps(params)}));"
    )
    out = subprocess.run(
        [node, "-e", prog], capture_output=True, text=True, timeout=30, encoding="utf-8",
        **no_console_creationflags(),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == sibling_split._cli(params)
