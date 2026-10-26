"""P-C regression: `tier-u-grant-cli grant` reports the grant it wrote, or refuses.

No real grant failure was named by C1, so this is a regression pin (not a replay).
Drives `main` end to end: a write the readback cannot confirm never reads as applied.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("tier_u_grant_cli_pc", str(_BIN_DIR / "tier-u-grant-cli.py"))
    spec = importlib.util.spec_from_loader("tier_u_grant_cli_pc", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


@pytest.fixture()
def wire():
    from coordinator_core.session import grant_directive

    saved = (_cli._import_module, grant_directive.write_tier_u_grant)

    def _apply(record, *, write=None, current="sess-1", path="/s/sess-1/tier-u-grant.json"):
        stub = SimpleNamespace(
            read_tier_u_grant=lambda *a, **k: record,
            core=SimpleNamespace(resolve_session_id=lambda cwd: current),
            _grant_file=lambda sid, cwd: Path(path),
        )
        _cli._import_module = lambda: stub
        _cli._grant_directive_module = lambda: grant_directive
        grant_directive.write_tier_u_grant = write or (lambda *a, **k: True)

    yield _apply
    _cli._import_module, grant_directive.write_tier_u_grant = saved


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(argv)
    out = buf.getvalue().strip()
    return rc, json.loads(out)


def _rec(**over):
    base = {"session_id": "sess-1", "granted_by": "pm", "note": "go", "ceremony": None}
    return {**base, **over}


def test_applied_names_path_and_scope(wire):
    wire(_rec())
    rc, reply = _run(["grant", "pm", "go"])
    assert rc == 0 and reply["ok"] is True
    assert reply["applied"][0]["path"] == str(Path("/s/sess-1/tier-u-grant.json"))
    assert reply["applied"][0]["scope"]["session_id"] == "sess-1"


@pytest.mark.parametrize(
    "record,reason",
    [
        (None, "readback"),
        (_rec(granted_by="ceremony", ceremony="x"), "scope"),
        (_rec(session_id="other"), "different session"),
    ],
)
def test_unconfirmed_write_is_refused_not_ok(wire, record, reason):
    wire(record)
    rc, reply = _run(["grant", "pm", "go"])
    assert rc == 1
    assert reply["ok"] is False and reply["applied"] == []
    assert reason in reply["refused"][0]["reason"]


def test_unknown_tier_refused_with_reason(wire):
    def _raise(*a, **k):
        raise ValueError("granted_by must be one of ['ceremony', 'pm']")

    wire(None, write=_raise)
    rc, reply = _run(["grant", "robot", "go"])
    assert rc == 2
    assert reply["ok"] is False and "granted_by" in reply["refused"][0]["reason"]
