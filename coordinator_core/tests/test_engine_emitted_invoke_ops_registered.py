"""Every `coordinator-invoke <op>` an engine source string tells an agent to run names a registered op.

The doctrine-prose ratchet (`test_doctrine_prose_op_names_resolve`) scans coordinator-content-repo pages only; an engine-emitted
brief or remedy that dials an unregistered op reaches the agent as "Method not found". The retest prompts once gave
a Windows agent a POSIX `PYTHONPATH=... python -m` line; it improvised `workflow.retest_targeted`, which exists
nowhere. The retest prompts now carry a PowerShell variant and say the command is a module, not an op.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from coordinator_core.tests.test_doctrine_prose_op_names_resolve import _CALL, _live_op_names

_ROOT = Path(__file__).resolve().parents[2]


def _engine_sources() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "coordinator_core/*.py"], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.split()
    return [_ROOT / f for f in out if "/tests/" not in f and not Path(f).name.startswith("test_")]


def test_engine_strings_invoke_only_registered_ops() -> None:
    live = _live_op_names()
    bad = []
    for path in _engine_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in _CALL.finditer(line):
                if m.group(1) not in live and not m.group(1).endswith((".py", ".json", ".md")):
                    bad.append(f"{path.relative_to(_ROOT).as_posix()}:{lineno} {m.group(1)}")
    assert not bad, "engine text tells an agent to invoke unregistered ops:\n  " + "\n  ".join(bad)


def test_retest_prompts_name_a_module_command_not_an_op() -> None:
    from coordinator_core.ops.dispatch_emit import ask_compose

    prompt = ask_compose._retest_await_call(head="h", anchor="a", repo_root=str(_ROOT), run_id="ask-20260101T000000-abcdef")
    assert re.search(r"PowerShell:\\n& ", prompt) and "not a coordinator-invoke op" in prompt
