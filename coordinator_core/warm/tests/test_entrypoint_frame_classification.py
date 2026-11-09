"""The warm client classifies the op a frame runs, not its `invoke.from_argv` wrapper."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.warm import client

_ALLOWLIST = Path(client.__file__).resolve().parents[1] / "ops" / "warm_entrypoint_allowlist.json"

#: Allowlisted entrypoints whose derived `<name>.<sub>` can hit a COMPUTE_ONLY row.
#: A new entry here goes retry-safe for that subcommand: add it only after confirming the
#: CLI's `<sub>` runs the op of the same name.
_ENTRYPOINTS_WITH_COMPUTE_ONLY_ROWS = frozenset(
    {"app-session", "baton-assemble", "cartography", "consolidate-assemble"}
)


def _entry_frame(entrypoint, argv) -> dict:
    return {"method": "invoke.from_argv", "params": {"entrypoint": entrypoint, "argv": argv}}


def _door_frame(argv) -> dict:
    return {"method": "invoke.from_argv", "params": {"argv": argv}}


def test_consolidate_brief_entrypoint_frame_is_retry_safe() -> None:
    frame = _entry_frame("consolidate-assemble", ["brief"])
    assert client._classified_op_name(frame) == "consolidate_assemble.brief"
    assert client._op_may_mutate(client._classified_op_name(frame)) is False


def test_consolidate_apply_entrypoint_frame_still_fails_closed() -> None:
    frame = _entry_frame("consolidate-assemble", ["apply"])
    assert client._classified_op_name(frame) == "consolidate_assemble.apply"
    assert client._op_may_mutate(client._classified_op_name(frame)) is True


@pytest.mark.parametrize("argv", [[], [""], [3], None, "brief"])
def test_unreadable_subcommand_answers_the_wrapper_method(argv) -> None:
    frame = _entry_frame("consolidate-assemble", argv)
    assert client._classified_op_name(frame) == "invoke.from_argv"
    assert client._op_may_mutate(client._classified_op_name(frame)) is True


def test_door_form_resolves_to_its_op() -> None:
    frame = _door_frame(["--repo", "x", "baton_assemble.brief", "{}"])
    assert client._classified_op_name(frame) == "baton_assemble.brief"
    assert client._op_may_mutate(client._classified_op_name(frame)) is False


def test_unregistered_door_op_fails_closed() -> None:
    assert client._op_may_mutate(client._classified_op_name(_door_frame(["no.such_op"]))) is True


def test_plain_method_frame_answers_its_method() -> None:
    assert client._classified_op_name({"method": "baton_assemble.brief", "params": {}}) == "baton_assemble.brief"


def test_compute_only_entrypoint_census_is_explicit() -> None:
    entrypoints = json.loads(_ALLOWLIST.read_text(encoding="utf-8"))["entrypoints"]
    colliding = {
        e
        for e in entrypoints
        for name, cls in OP_CLASSIFICATION.items()
        if cls is OpClass.COMPUTE_ONLY and name.startswith(e.replace("-", "_") + ".")
    }
    assert colliding == _ENTRYPOINTS_WITH_COMPUTE_ONLY_ROWS
