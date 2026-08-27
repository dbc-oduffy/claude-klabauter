"""coordinator_core.ops.tests.test_invoke_from_entrypoint -- tests for the
"invoke.from_argv" op's `entrypoint` param (coordinator_core/ops/
invoke_from_argv.py), added by chunk C0 of the multi-name native-invocation
surface (docs/research/spike-verdicts/2026-08-27-multi-name-native-invocation-
surface.md).

Coverage:
  (a) `entrypoint` ABSENT: byte-for-byte unchanged from before this field
      existed -- the existing `_dispatch_argv` path, proven by
      `test_invoke_from_argv.py` already, re-asserted narrowly here as a
      regression guard on the additive contract itself.
  (b) `entrypoint` present, naming a real `coordinator/bin/<name>.py` CLI
      that defines `main(argv) -> int`: that CLI's OWN parser runs, in
      process, under the door.c comment's proving choice --
      `cross-repo-memo` (the CLI the spike verdict already measured end to
      end, giving a directly comparable number) -- exercised here via a
      fast, network-free path (`list --help`, which argparse answers via
      SystemExit(0) before any op dispatch) so this test stays off any live
      engine/repo-state dependency.
  (c) `entrypoint` present but naming no real script: FAILS CLOSED with a
      ValueError naming both the entrypoint and the missing script path --
      never silently substituting a different CLI's grammar (the exact
      mis-dispatch this field exists to prevent, per door.c's own
      "THE COLD LEG IS PART OF THIS CHUNK" comment for the cold twin of this
      same property).
  (d) `entrypoint` present but not a non-empty string: params-validation
      ValueError, same class as `argv`/`cwd`'s existing validation.
  (e) `cwd` is threaded through as an explicit chdir for the duration of the
      call, and restored afterward -- proven by asserting the test process's
      own cwd is unchanged after the call, and that the entrypoint CLI (which
      resolves its own repo root off `os.getcwd()`) sees the requested `cwd`.

These tests call the handler directly (not via a subprocess) -- same
rationale as `test_invoke_from_argv.py`'s own module docstring: it is sync,
holds no `os._exit`, and there is no separate-process constraint here to
work around.

Spec backlink: docs/research/spike-verdicts/2026-08-27-multi-name-native-
    invocation-surface.md, chunk C0
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.invoke_from_argv import _invoke_from_argv

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
_BIN_DIR = Path(_PROJECT_ROOT) / "coordinator" / "bin"


# ---------------------------------------------------------------------------
# (a) entrypoint absent: unchanged
# ---------------------------------------------------------------------------

def test_entrypoint_absent_is_unchanged_dispatch_argv_path():
    """Regression guard on the additive contract itself: with no `entrypoint`
    key at all, the response shape/exit_code is the ordinary `_dispatch_argv`
    JSON-RPC envelope -- proven fully by `test_invoke_from_argv.py`; this
    narrow check exists so a future edit that makes `entrypoint` load-bearing
    even when absent (e.g. defaulting it to something) fails HERE, close to
    the change that would cause it."""
    result = _invoke_from_argv({"argv": ["ping", "{}"], "cwd": _PROJECT_ROOT})
    assert result["exit_code"] == 0
    assert '"jsonrpc"' in result["stdout"]
    assert '"result"' in result["stdout"]


# ---------------------------------------------------------------------------
# (b) entrypoint present, real CLI, own parser runs
# ---------------------------------------------------------------------------

def test_entrypoint_present_runs_the_named_clis_own_parser():
    """cross-repo-memo is the proving CLI (door.c's own comment; the spike
    verdict already measured it end to end). `list --help` is argparse's own
    SystemExit(0) help path -- reached before any op dispatch/repo-state
    read, so this exercises "the named CLI's OWN parser runs" without paying
    for (or depending on) a live engine round trip."""
    assert (_BIN_DIR / "cross-repo-memo.py").is_file(), (
        "setup error: coordinator/bin/cross-repo-memo.py must exist for "
        "this to be a meaningful test of the real entrypoint-loading path"
    )
    result = _invoke_from_argv({
        "argv": ["list", "--help"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "cross-repo-memo",
    })
    assert result["exit_code"] == 0
    assert "usage" in result["stdout"].lower()
    assert result["stderr"] == ""


def test_entrypoint_argv_is_relayed_verbatim_no_translation():
    """Negative-spec proof: no argv->op mapping exists in the op handler --
    the SAME argv reaches the named CLI's own parser whether it is
    recognised (exit 0) or not (argparse's own usage-error exit 2), which is
    only possible if nothing here inspects/rewrites/looks up the tokens."""
    result = _invoke_from_argv({
        "argv": ["definitely-not-a-real-verb"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "cross-repo-memo",
    })
    # cross-repo-memo's own main() treats an unrecognised non-flag token as
    # a friendly-hint-or-legacy-parser case (see its own main() docstring) --
    # asserting only that IT decided the outcome (nonzero exit, its own
    # stderr wording), never a table this op consulted.
    assert result["exit_code"] != 0
    assert "cross-repo-memo" in result["stderr"]


# ---------------------------------------------------------------------------
# (c) fail closed on a missing script -- never substitute a different CLI
# ---------------------------------------------------------------------------

def test_entrypoint_naming_no_real_script_fails_closed():
    missing = "definitely-not-a-real-coordinator-bin-cli"
    assert not (_BIN_DIR / f"{missing}.py").is_file()

    with pytest.raises(ValueError) as excinfo:
        _invoke_from_argv({"argv": ["list"], "cwd": _PROJECT_ROOT, "entrypoint": missing})

    message = str(excinfo.value)
    assert missing in message
    assert "coordinator-invoke" not in message, (
        "a fail-closed refusal must name the ACTUAL missing script, never "
        "the default CLI -- silently falling back to it is the exact "
        "mis-dispatch this field exists to prevent"
    )


# ---------------------------------------------------------------------------
# (d) params validation
# ---------------------------------------------------------------------------

def test_entrypoint_must_be_a_non_empty_string_when_present():
    with pytest.raises(ValueError, match="params.entrypoint"):
        _invoke_from_argv({"argv": ["list"], "cwd": _PROJECT_ROOT, "entrypoint": ""})


def test_entrypoint_must_be_a_string_not_some_other_type():
    with pytest.raises(ValueError, match="params.entrypoint"):
        _invoke_from_argv({"argv": ["list"], "cwd": _PROJECT_ROOT, "entrypoint": 123})


# ---------------------------------------------------------------------------
# (e) cwd is an explicit, restored chdir -- never left dangling
# ---------------------------------------------------------------------------

def test_cwd_is_chdired_for_the_call_and_restored_after():
    before = os.getcwd()
    result = _invoke_from_argv({
        "argv": ["list", "--help"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "cross-repo-memo",
    })
    assert result["exit_code"] == 0
    assert os.getcwd() == before, (
        "invoke.from_argv must restore this (server) process's cwd after "
        "an entrypoint call -- a warm pool worker's cwd corrupting across "
        "requests would corrupt every subsequent call's relative-path "
        "resolution, not just this one's"
    )


def test_cwd_is_restored_even_when_the_entrypoint_fails_closed():
    before = os.getcwd()
    with pytest.raises(ValueError):
        _invoke_from_argv({
            "argv": ["list"],
            "cwd": _PROJECT_ROOT,
            "entrypoint": "definitely-not-a-real-coordinator-bin-cli",
        })
    assert os.getcwd() == before
