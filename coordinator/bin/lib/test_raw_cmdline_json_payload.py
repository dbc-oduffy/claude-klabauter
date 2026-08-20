"""JSON-payload recovery through a `.cmd` forwarder, plus enrolment-set sync.

`cmd.exe` strips a JSON payload's double quotes while populating a `.cmd`
launcher's `%*`, so `--decisions '<json>'` reaches the CLI as
`{lesson:{disposition:skip}}` and is rejected as malformed -- a payload that
was well-formed when sent. Shape W (the `.cmd` sibling) is the rung
`resolve-coordinator-bin.md` mandates on Windows, so the documented
invocation shape and the JSON-argument surface were mutually exclusive there
(cross-repo/inbox/2026-08-20-doe-claude-em-cmd-forwarder-eats-json-and-two-
smaller-seams.md, item 1).

`recover_windows_argv` cannot fix this: it re-tokenizes the raw tail with
`shlex` and bails on token-count disagreement, and a JSON payload's nested
quotes split into several tokens where argv has one. `recover_json_flag_argv`
recovers the named flag's value by EXTENT instead, which is what these tests
pin.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import entry_point_shim  # noqa: E402
import raw_cmdline_recovery as rcr  # noqa: E402

_PAYLOAD = '{"j-kind": {"disposition": "ack-nil"}, "other": {"n": 1}}'


def _mangled(payload: str) -> str:
    """What cmd.exe hands the process: every double quote gone."""
    return payload.replace('"', "")


def _seed_capture(tmp_path, monkeypatch, raw_text: str) -> None:
    d = tmp_path / "_coordinator_launcher_test"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "cmdline.tmp"
    f.write_text(raw_text, encoding="utf-8")
    monkeypatch.setenv(rcr.RAW_CMDLINE_FILE_ENV, str(f))


def _sound_raw(launcher: str, payload: str) -> str:
    """A PowerShell-shaped spawn: the whole post-`/c` string outer-quoted."""
    return (
        r'C:\Windows\system32\cmd.exe /c ""C:\bin\%s" brief foo --decisions %s""'
        % (launcher, payload)
    )


class TestExtractBalancedJson:
    def test_extracts_a_nested_object(self):
        assert rcr._extract_balanced_json('"%s"' % _PAYLOAD) == _PAYLOAD

    def test_a_brace_inside_a_string_does_not_terminate_it(self):
        payload = '{"note": "closing } brace"}'
        assert rcr._extract_balanced_json(payload) == payload

    def test_an_escaped_quote_does_not_end_the_string(self):
        payload = '{"note": "a \\" quote}"}'
        assert rcr._extract_balanced_json(payload) == payload

    def test_returns_none_when_no_container_opens(self):
        assert rcr._extract_balanced_json(" plain-value") is None

    def test_returns_none_when_never_closed(self):
        assert rcr._extract_balanced_json('{"a": {') is None


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe argv mangling is Windows-only")
class TestRecoverJsonFlagArgv:
    def test_restores_a_stripped_payload(self, tmp_path, monkeypatch):
        argv = ["brief", "foo", "--decisions", _mangled(_PAYLOAD)]
        _seed_capture(tmp_path, monkeypatch, _sound_raw("pickup-assemble.cmd", '"%s"' % _PAYLOAD))

        out = rcr.recover_json_flag_argv(argv, "pickup-assemble.cmd", ("--decisions",))

        assert json.loads(out[3]) == json.loads(_PAYLOAD)
        assert out[:3] == argv[:3]

    def test_leaves_argv_alone_when_the_transport_is_unsound(self, tmp_path, monkeypatch):
        """A non-outer-quoted spawn (git-bash, `subprocess.run([...])`) hands
        us text the quotes are already gone from -- nothing to recover, and
        recovery must not invent one."""
        argv = ["brief", "foo", "--decisions", _mangled(_PAYLOAD)]
        _seed_capture(
            tmp_path,
            monkeypatch,
            r"C:\Windows\system32\cmd.exe /c C:\bin\pickup-assemble.cmd brief foo",
        )

        assert rcr.recover_json_flag_argv(argv, "pickup-assemble.cmd", ("--decisions",)) == argv

    def test_never_raises_on_an_unsound_transport(self, tmp_path, monkeypatch):
        """Unlike `recover_windows_argv`, which raises for its refusing
        consumers. These CLIs are called from tests and in-repo subprocess
        callers on exactly that transport, passing argv that was never
        mangled."""
        _seed_capture(
            tmp_path,
            monkeypatch,
            r"C:\Windows\system32\cmd.exe /c C:\bin\pickup-assemble.cmd brief",
        )
        rcr.recover_json_flag_argv(["brief"], "pickup-assemble.cmd", ("--decisions",))

    def test_does_not_substitute_a_value_that_is_not_valid_json(self, tmp_path, monkeypatch):
        argv = ["brief", "--decisions", "{a b c}"]
        _seed_capture(
            tmp_path, monkeypatch, _sound_raw("pickup-assemble.cmd", '"{a b c}"')
        )

        assert rcr.recover_json_flag_argv(argv, "pickup-assemble.cmd", ("--decisions",)) == argv

    def test_absent_capture_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv(rcr.RAW_CMDLINE_FILE_ENV, raising=False)
        argv = ["brief", "--decisions", "{}"]
        assert rcr.recover_json_flag_argv(argv, "pickup-assemble.cmd", ("--decisions",)) == argv

    def test_a_launcher_name_not_in_the_capture_is_a_no_op(self, tmp_path, monkeypatch):
        argv = ["brief", "--decisions", _mangled(_PAYLOAD)]
        _seed_capture(tmp_path, monkeypatch, _sound_raw("some-other.cmd", '"%s"' % _PAYLOAD))

        assert rcr.recover_json_flag_argv(argv, "pickup-assemble.cmd", ("--decisions",)) == argv


class TestEnrolmentSetsStayInSync:
    """Three sets name the JSON-payload targets and must agree: without the
    generator rows the launcher emits no capture at all and the shim's
    recovery is a silent no-op; without the shim row the capture is written
    and never read."""

    def test_shim_targets_are_all_enrolled_in_both_generators(self):
        import re

        bin_dir = Path(entry_point_shim.__file__).resolve().parent.parent
        gen = (bin_dir / "gen-launcher-shim.py").read_text(encoding="utf-8")
        substrate = (
            bin_dir.parent.parent / "coordinator_core" / "install" / "substrate.py"
        ).read_text(encoding="utf-8")

        gen_set = set(re.findall(r'"coordinator/bin/([a-z0-9-]+)\.py"', gen))
        sub_set = set(re.findall(r'"([a-z0-9-]+)\.py"', substrate))

        missing_gen = entry_point_shim._JSON_PAYLOAD_TARGETS - gen_set
        missing_sub = entry_point_shim._JSON_PAYLOAD_TARGETS - sub_set
        assert not missing_gen, f"not enrolled in gen-launcher-shim: {sorted(missing_gen)}"
        assert not missing_sub, f"not enrolled in substrate: {sorted(missing_sub)}"

    def test_every_json_payload_target_is_a_real_assemble_target(self):
        unknown = entry_point_shim._JSON_PAYLOAD_TARGETS - set(entry_point_shim.ASSEMBLE_TARGETS)
        assert not unknown, f"not dispatchable names: {sorted(unknown)}"

    def test_the_target_set_is_not_vacuous(self):
        assert "pickup-assemble" in entry_point_shim._JSON_PAYLOAD_TARGETS
        assert "workstream-complete-assemble" in entry_point_shim._JSON_PAYLOAD_TARGETS


class TestShimRecoveryIsFailSafe:
    def test_an_unenrolled_target_is_untouched(self):
        argv = ["brief", "--decisions", "{a: b}"]
        assert entry_point_shim._recover_json_payload_argv("orient-assemble", argv) == argv

    def test_recovery_failure_never_breaks_the_invocation(self, monkeypatch):
        """Any exception out of the recovery half returns the caller's argv
        untouched -- an invocation must never fail because a best-effort
        diagnostic aid did."""

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated recovery failure")

        monkeypatch.setattr(rcr, "recover_json_flag_argv", _boom)
        argv = ["brief", "--decisions", "{a: b}"]
        assert entry_point_shim._recover_json_payload_argv("pickup-assemble", argv) == argv
