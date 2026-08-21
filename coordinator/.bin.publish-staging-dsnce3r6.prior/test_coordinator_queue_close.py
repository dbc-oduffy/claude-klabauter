"""test_coordinator_queue_close.py — pytest suite for coordinator-queue-close.

Hermetic by construction: `sys.modules["cc_invoke"]` is pre-seeded with a fake
module BEFORE the subject is imported, so the subject's
`from cc_invoke import RouteMutationError, route_mutation` short-circuits on
the cached entry and no live engine-root resolution, no
`coordinator_core.invoke` subprocess, and no git spawn ever happens. Same
mechanism (and the same never-leave-the-fake-installed discipline) as
`test_close_origin_stub_on_ship.py`, which is this tree's convention for a
`coordinator/bin/` op-forwarder suite.

NOTHING here touches a real queue entry, a real archive directory, or git.
`queue.close` stamps, COMMITS, and archives on the real tree — a test that
reached the live op would close a real improvement-queue entry as a side
effect, which is unrecoverable-by-accident. The op is therefore stubbed in
every test; the only filesystem this suite touches is pytest's own `tmp_path`.

Coverage:
  - argument parsing: entry_path is positional, --closed-by is required,
    --closed-at is shape-validated and omitted from params when absent
  - the params the subject actually hands the op (op name + exact param dict)
  - exit-code mapping: successful close -> 0; op-reported error
    (RouteMutationError) -> 1; transport RuntimeError -> 1
  - skipped_reason is surfaced, never swallowed
  - the total-no-op discriminator: archived-copy-on-disk -> 0 (replay),
    nothing anywhere -> 1 (bad path)

Spec backlink: coordinator_core/ops/queue_close.py
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "coordinator-queue-close")

_ABSENT = object()


class _FakeRouteMutationError(RuntimeError):
    """Stand-in for cc_invoke.RouteMutationError — carries `.result` like the real one."""

    def __init__(self, message: str, result: dict | None = None) -> None:
        super().__init__(message)
        self.result = result


def _install_fake_cc_invoke(route_mutation_fn):
    """Seed `sys.modules["cc_invoke"]` with a fake exposing route_mutation and
    RouteMutationError; return the prior entry for `_restore_cc_invoke`.

    Negative spec: `sys.modules` is process-global and 30+ `coordinator/bin/`
    scripts import `cc_invoke` by bare name. A fake left installed past the
    test that seeded it makes every later such import in the same worker
    resolve against a two-attribute stub, surfacing as an ImportError in an
    unrelated file. Never install without a paired restore.
    """
    fake = types.ModuleType("cc_invoke")
    fake.route_mutation = route_mutation_fn
    fake.RouteMutationError = _FakeRouteMutationError
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    """Undo `_install_fake_cc_invoke`, restoring absence as absence."""
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    """Import a brand-new copy of the subject so per-test fakes never leak.

    Explicit `SourceFileLoader`: the subject is EXTENSIONLESS, and
    `spec_from_file_location` infers its loader from the filename suffix — for
    a file with no recognized Python suffix it yields a spec with no loader.
    Naming the loader is this tree's standing idiom for its extensionless CLIs
    (see `coordinator/tests/test_queue_triage_cli.py`, `test_lesson_promote.py`,
    and ~40 siblings).
    """
    module_name = "coordinator_queue_close_under_test"
    sys.modules.pop(module_name, None)
    loader = importlib.machinery.SourceFileLoader(module_name, SUBJECT_PATH)
    spec = importlib.util.spec_from_file_location(module_name, SUBJECT_PATH, loader=loader)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_main(route_mutation_fn, argv, repo_root="/nonexistent-fake-repo-root"):
    """Load a fresh subject against the given fake op, run main(argv), and
    capture (exit_code, stdout, stderr).

    `_repo_root` is replaced rather than left live: the real one spawns
    `git rev-parse`, and the no-op discriminator in `_render` probes
    `<repo_root>/<dest>` on disk — pinning it keeps both off the real tree.
    """
    prior = _install_fake_cc_invoke(route_mutation_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        subject._repo_root = lambda: str(repo_root)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main(argv)
    finally:
        _restore_cc_invoke(prior)
    return code, out.getvalue(), err.getvalue()


def _ok(**overrides) -> dict:
    """A queue.close success envelope, overridable field-by-field."""
    reply = {
        "closed": True,
        "archived": True,
        "dest": "archive/improvement-queue/2026-08/entry.yaml",
        "committed_sha": "0123456789abcdef",
        "skipped_reason": None,
        "resumed": False,
        "exit_code": 0,
        "error": None,
    }
    reply.update(overrides)
    return reply


ENTRY = "state/improvement-queue/2026-08-11-some-entry-9b6d190ee07e.yaml"


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


# ---------------------------------------------------------------------------
# Argument parsing + the params actually handed to the op
# ---------------------------------------------------------------------------


def test_params_handed_to_the_op():
    seen = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        seen["op"] = op
        seen["params"] = params
        seen["repo_root"] = repo_root
        return _ok()

    code, _out, _err = _run_main(
        _route_mutation, [ENTRY, "--closed-by", "a8fa206de", "--closed-at", "2026-08-11"]
    )

    assert code == 0
    assert seen["op"] == "queue.close"
    assert seen["params"] == {
        "entry_path": ENTRY,
        "closed_by": "a8fa206de",
        "closed_at": "2026-08-11",
    }
    assert seen["repo_root"] == "/nonexistent-fake-repo-root"


def test_closed_at_omitted_when_not_given():
    """Absent --closed-at must not appear in params at all: the op defaults it
    to today (UTC) itself, and an explicit None would be a different value."""
    seen = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        seen["params"] = params
        return _ok()

    code, _out, _err = _run_main(_route_mutation, [ENTRY, "--closed-by", "a8fa206de"])

    assert code == 0
    assert "closed_at" not in seen["params"]


def test_closed_by_is_required():
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return _ok()

    with pytest.raises(SystemExit) as excinfo:
        _run_main(_route_mutation, [ENTRY])

    assert excinfo.value.code == 2
    assert reached["called"] is False


def test_entry_path_is_required():
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return _ok()

    with pytest.raises(SystemExit) as excinfo:
        _run_main(_route_mutation, ["--closed-by", "a8fa206de"])

    assert excinfo.value.code == 2
    assert reached["called"] is False


def test_malformed_closed_at_is_a_usage_error_before_routing():
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return _ok()

    code, _out, err = _run_main(
        _route_mutation, [ENTRY, "--closed-by", "a8fa206de", "--closed-at", "11-08-2026"]
    )

    assert code == 2
    assert "--closed-at" in err
    assert reached["called"] is False


# ---------------------------------------------------------------------------
# Exit-code mapping
# ---------------------------------------------------------------------------


def test_successful_close_exits_zero_and_renders_the_envelope():
    def _route_mutation(op, params, repo_root, legacy_fn):
        return _ok()

    code, out, err = _run_main(_route_mutation, [ENTRY, "--closed-by", "a8fa206de"])

    assert code == 0
    assert "closed=True" in out
    assert "archived=True" in out
    assert "archive/improvement-queue/2026-08/entry.yaml" in out
    assert "0123456789abcdef" in out
    assert err == ""


def test_op_reported_error_exits_one():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeRouteMutationError(
            "op refused: exit_code=1",
            {
                "closed": False,
                "archived": False,
                "dest": None,
                "committed_sha": None,
                "exit_code": 1,
                "error": "stamp lock timeout",
            },
        )

    code, _out, err = _run_main(_route_mutation, [ENTRY, "--closed-by", "a8fa206de"])

    assert code == 1
    assert "refused" in err
    assert "exit_code=1" in err


def test_op_error_after_a_landed_stamp_commit_surfaces_the_sha():
    """The stamp commit can land durably while the archival leg fails. That SHA
    is the only record of what DID happen — it must not be lost in the failure
    path."""

    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeRouteMutationError(
            "op refused: exit_code=1",
            _ok(archived=False, dest=None, exit_code=1, error="archive delegation raised: boom"),
        )

    code, _out, err = _run_main(_route_mutation, [ENTRY, "--closed-by", "a8fa206de"])

    assert code == 1
    assert "0123456789abcdef" in err


def test_transport_failure_exits_one():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure (rc=127)")

    code, _out, err = _run_main(_route_mutation, [ENTRY, "--closed-by", "a8fa206de"])

    assert code == 1
    assert "transport failed" in err


# ---------------------------------------------------------------------------
# skipped_reason must never be swallowed
# ---------------------------------------------------------------------------


def test_deferred_skip_is_surfaced_not_swallowed():
    def _route_mutation(op, params, repo_root, legacy_fn):
        return _ok(
            closed=False, archived=False, dest=None, committed_sha=None,
            skipped_reason="deferred",
        )

    code, out, err = _run_main(_route_mutation, [ENTRY, "--closed-by", "a8fa206de"])

    assert code == 0
    assert "closed=False" in out
    assert "DECLINED (deferred)" in err
    assert "status: open" in err


def test_resumed_stranded_write_is_reported():
    def _route_mutation(op, params, repo_root, legacy_fn):
        return _ok(resumed=True)

    code, out, _err = _run_main(_route_mutation, [ENTRY, "--closed-by", "a8fa206de"])

    assert code == 0
    assert "resumed" in out


# ---------------------------------------------------------------------------
# The total-no-op discriminator
# ---------------------------------------------------------------------------


def test_idempotent_replay_with_an_archived_copy_on_disk_exits_zero(tmp_path):
    """closed=False + archived=False, but the archive destination EXISTS: the
    entry is already closed and swept. A legitimate replay, exit 0."""
    dest_rel = "archive/improvement-queue/2026-08/entry.yaml"
    dest = tmp_path / dest_rel
    dest.parent.mkdir(parents=True)
    dest.write_text("status: closed\n", encoding="utf-8")

    def _route_mutation(op, params, repo_root, legacy_fn):
        return _ok(closed=False, archived=False, dest=dest_rel, committed_sha=None)

    code, out, err = _run_main(
        _route_mutation, [ENTRY, "--closed-by", "a8fa206de"], repo_root=tmp_path
    )

    assert code == 0
    assert "already archived" in out
    assert err == ""


def test_total_no_op_with_nothing_on_disk_exits_one(tmp_path):
    """closed=False + archived=False and NO archived copy either: the path names
    no entry at all. Exiting 0 here is the silent no-op this CLI must never
    produce."""

    def _route_mutation(op, params, repo_root, legacy_fn):
        return _ok(
            closed=False, archived=False,
            dest="archive/improvement-queue/2026-08/entry.yaml", committed_sha=None,
        )

    code, _out, err = _run_main(
        _route_mutation, [ENTRY, "--closed-by", "a8fa206de"], repo_root=tmp_path
    )

    assert code == 1
    assert "nothing to close" in err
    assert ENTRY in err
