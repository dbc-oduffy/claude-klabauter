"""Every brief-half's transport-failure path (exit 3) emits nothing on
stdout — completion-evidence contract (DR-442, `docs/plans/2026-09-23-
completion-evidence-contract.md` C5). Exit 3 means compute never ran: the
diagnostic goes to stderr, and the exit code is the only evidence. A
synthetic stdout envelope on this path would assert a decision no one
computed.

Census at HEAD (`grep -rln 'EXIT_TRANSPORT_FAIL *= *3' coordinator_core
--include=*.py | grep -v /tests/`), `contract/apply_base.py` excluded
(apply-side, per the row). `merge_assemble` has no `brief` CLI verb (its
`brief()` transport branch is reachable only through `apply`, per K-114 —
DoE-claude `coordinator/skills/merging-to-main/SKILL.md` @ 62d63adfe), so
it is out of this rule's population and carries no monkeypatch case here.
`pickup_assemble/__init__.py` no longer computes `brief` at all (moved to
`pickup_brief.py`), so it too carries no case of its own; `pickup_brief.py`
covers the population. `backlog_grind_assemble` and `consolidate_assemble`
are already conformant and serve as positive controls.
"""

from __future__ import annotations

import builtins
import importlib
from typing import Any

import pytest

CASES: list[tuple[str, str, list[str], str]] = [
    ("coordinator_core.backlog_grind_assemble", "brief", ["brief", "bug-blitz"], "RuntimeError"),
    ("coordinator_core.consolidate_assemble", "brief", ["brief"], "RuntimeError"),
    (
        "coordinator_core.learn_lessons_assemble",
        "brief",
        ["some/wiki/path.md", "incoming candidate text"],
        "RuntimeError",
    ),
    (
        "coordinator_core.staff_session_assemble",
        "resolve_roster",
        ["--session-mode", "plan", "--domain-signal", "x"],
        "RuntimeError",
    ),
    ("coordinator_core.roadmap_planning_assemble", "brief", [], "RuntimeError"),
    ("coordinator_core.sprint_planning_assemble", "brief", [], "RuntimeError"),
    ("coordinator_core.pickup_brief", "brief_multi", ["brief", "some/artifact/path.md"], "RuntimeError"),
    (
        "coordinator_core.baton_assemble",
        "brief",
        ["brief", "handoff", "some-slug"],
        "TransportFailure",
    ),
    (
        "coordinator_core.sizing_assemble",
        "route",
        ["--tshirt", "M"],
        "RuntimeError",
    ),
    ("coordinator_core.workstream_complete", "brief", ["brief"], "RuntimeError"),
]


_IDS = [module_path.rsplit(".", 1)[-1] for module_path, _, _, _ in CASES]


@pytest.mark.parametrize("module_path,attr,argv,exc_name", CASES, ids=_IDS)
def test_transport_failure_path_emits_nothing_on_stdout(
    module_path: str,
    attr: str,
    argv: list[str],
    exc_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module(module_path)
    exc_type = getattr(module, exc_name, None) or getattr(builtins, exc_name)

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise exc_type("simulated transport failure")

    monkeypatch.setattr(module, attr, _raise)

    exit_code = module.main(argv)

    captured = capsys.readouterr()
    assert exit_code == module.EXIT_TRANSPORT_FAIL == 3
    assert captured.out == ""
    assert captured.err != ""


def test_merge_assemble_has_no_brief_cli_verb() -> None:
    merge_assemble = importlib.import_module("coordinator_core.merge_assemble")

    exit_code = merge_assemble.main(["brief"])

    assert exit_code == merge_assemble.EXIT_USAGE


def test_pickup_assemble_no_longer_computes_brief() -> None:
    pickup_assemble = importlib.import_module("coordinator_core.pickup_assemble")

    exit_code = pickup_assemble.main(["brief", "some/artifact/path.md"])

    assert exit_code == pickup_assemble.EXIT_USAGE
