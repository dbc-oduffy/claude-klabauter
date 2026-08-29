"""coordinator/bin/tests/test_warm_door_argv_survives_first_arg.py -- regression
for the C5-disposition-adjacent warm-door argv-truncation bug: a `main(argv)`
that did `argv[1:]` when called the way the WARM DOOR calls it (program-name-free
argv -- `coordinator_core/ops/invoke_from_argv.py :: _invoke_from_argv`'s own
`argv` param, e.g. `["brief"]` for `entrypoint="quick-wrap-assemble"`) silently
discarded the caller's first real argument, because that slice is only correct
when the CLI path prepends a program-name placeholder first.

Exercises `coordinator/bin/quick-wrap-assemble.py::main` directly -- the shape
of the call `_run_entrypoint` makes in-process, not `sys.argv` -- as a
representative of the seven sibling `-assemble.py`/`autonomous-verb.py`/
`assert-no-dangling-plan-backlinks.py` files that shared this defect (staff-eng
review, 2026-08-29). One file is enough coverage per the review's own ask;
`main(argv)` treats `argv` as user args and must NOT slice it.

Negative-spec: does NOT exercise the real warm transport (UDS, JSON-RPC) or
`run_target`'s own body -- `entry_point_shim.run_target` is monkeypatched to a
spy so this test isolates the ONE property in question (does the first
warm-door argument reach the target), not the whole assembler pipeline.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_DIR = _REPO_ROOT / "coordinator" / "bin" / "lib"
_TARGET = _REPO_ROOT / "coordinator" / "bin" / "quick-wrap-assemble.py"

for _p in (str(_LIB_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_point_shim  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location("quick_wrap_assemble_cli", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cli_mod():
    return _load_module()


def test_warm_door_call_shape_preserves_first_argument(monkeypatch, cli_mod):
    """`_run_entrypoint` calls `main_fn(argv)` with a program-name-free argv --
    e.g. `main(["brief"])` -- not `main(sys.argv)`. `main` must hand
    `run_target` that SAME list, not `argv[1:]` (which would drop "brief" and
    leave `run_target` with an empty argv)."""
    seen = {}

    def _spy_run_target(name, argv):
        seen["name"] = name
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(entry_point_shim, "run_target", _spy_run_target)

    rc = cli_mod.main(["brief"])

    assert rc == 0
    assert seen["argv"] == ["brief"], (
        f"warm-door call main(['brief']) must reach run_target with ['brief'], "
        f"got {seen.get('argv')!r} -- the first real argument was dropped by an "
        "argv[1:] slice inside main()"
    )


def test_cli_entry_still_strips_the_program_name(monkeypatch, cli_mod):
    """The `if __name__ == '__main__':` leg passes `sys.argv[1:]` to `main` --
    covered here by calling `main` the way that leg does, confirming the CLI
    door still receives real user args (not a further-sliced, empty list) when
    fed a program-name-free argv, same as the warm door."""
    seen = {}

    def _spy_run_target(name, argv):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(entry_point_shim, "run_target", _spy_run_target)

    rc = cli_mod.main(["brief", "--foo", "bar"])

    assert rc == 0
    assert seen["argv"] == ["brief", "--foo", "bar"]
