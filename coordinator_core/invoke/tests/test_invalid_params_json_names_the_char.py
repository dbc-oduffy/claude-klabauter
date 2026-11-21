"""A malformed params payload names the offending char and points at --params-file.

Run 9f094f worker C5 sent a plan.tasks.mutate evidence-append whose inline JSON text carried a
Windows path; the parse failed twice with a bare "Expecting ',' delimiter" column. The error now
names the char and its neighbourhood, and the row prompt that teaches evidence-append teaches
the file form, which no shell can re-quote.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.dispatch_emit.emit import _row_prompt
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_PLAN = "docs/plans/2026-11/2026-11-20-hnd-fresh-unregistered-store-paths-bd1fa3.md"
BS = chr(92)
_TEXT = (
    f"Overlay verified at C:{BS}Users{BS}example-operator{BS}Temp{BS}ovl{BS} and "
    f"C:{BS}dev{BS}example-retrieval-repo{BS}scratch{BS}ovl{BS}, sha256 DA74A9A8 matches."
)


def _message(stderr: str) -> str:
    return json.loads(stderr.strip())["error"]["message"]


def _inline(tmp_path: Path, payload: str) -> str:
    _, stderr, exit_code = _dispatch_argv(
        ["plan.tasks.mutate", payload], str(tmp_path), allow_warm=False
    )
    assert exit_code == 1
    return _message(stderr)


def test_windows_path_ending_in_backslash_names_the_char_and_the_file_form(tmp_path):
    # A path's trailing backslash before the closing quote reads as an escaped quote, so the
    # string never closes and the parse fails at the next key: C5's "Expecting ',' delimiter".
    payload = (
        '{"verb":"evidence-append","plan_path":"' + _PLAN + '","id":"C5","text":"'
        + f"C:{BS}dev{BS}x{BS}" + '","note":"done"}'
    )
    message = _inline(tmp_path, payload)
    assert message.startswith("Invalid params_json: ")
    assert "near '" in message
    assert "escapes the closing quote" in message
    assert "--params-file <path>" in message


def test_shell_stripped_quotes_name_the_found_char(tmp_path):
    payload = '{verb:evidence-append,"text":"C:' + BS + 'dev"}'
    message = _inline(tmp_path, payload)
    assert "Expecting property name enclosed in double quotes at char 1" in message
    assert "found 'v'" in message


def test_truncated_payload_says_end_of_input(tmp_path):
    message = _inline(tmp_path, '{"verb":"evidence-append","text":"C:')
    assert "found end of input" in message or "Unterminated string" in message
    assert "char " in message


def test_the_same_payload_through_params_file_parses_with_its_windows_path(tmp_path):
    params = {"verb": "nope-not-a-verb", "plan_path": _PLAN, "id": "C5", "text": _TEXT}
    path = tmp_path / "evidence-params.json"
    path.write_text(json.dumps(params), encoding="utf-8")
    _, stderr, _ = _dispatch_argv(
        ["plan.tasks.mutate", "--params-file", str(path)], str(tmp_path), allow_warm=False
    )
    assert "Invalid params_json" not in stderr


def test_evidence_row_prompt_teaches_the_params_file_form():
    row = WaveRow(
        id="C5",
        title="audit",
        surface="docs/plans/p.evidence.yaml",
        writes=["docs/plans/p.evidence.yaml"],
        reads=[],
        depends_on=[],
    )
    prompt = _row_prompt(row, "docs/plans/p.md")
    assert "evidence-append" in prompt
    assert "--params-file <path>" in prompt
