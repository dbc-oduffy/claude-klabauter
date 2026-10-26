"""P-H9 replay: the same L ask floors to the same size and rows, and sizing-assemble refuses XS/S/M for it."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "sizing-assemble.py"

L_ASK = (
    "P-A ledger of parts; P-B receipts; P-C mutation truth; P-D fold bar; "
    "P-F1 anchor gate; P-F2 reviewer verdict; P-F3 refire; P-G6 executor heavy."
)


def _main(argv):
    import sys

    if str(_BIN.parent) not in sys.path:
        sys.path.insert(0, str(_BIN.parent))
    spec = importlib.util.spec_from_file_location("sizing_assemble_bin_ph9", _BIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(argv)


def _floor(capsys, ask=L_ASK):
    rc = _main(["--floor", "--intent", ask])
    return rc, capsys.readouterr().out


def test_same_l_ask_yields_identical_fingerprint_every_time(capsys):
    outs = []
    for _ in range(3):
        rc, out = _floor(capsys)
        assert rc == 0
        outs.append(out)
    assert len(set(outs)) == 1
    fp = json.loads(outs[0])
    assert fp["tshirt_floor"] == "L"
    assert fp["parts_count"] == 8
    assert fp["rows"] == sorted(fp["rows"])


@pytest.mark.parametrize("shirt", ["XS", "S", "M"])
def test_tshirt_below_floor_is_refused(shirt, capsys):
    assert _main(["--appetite", "large", "--tshirt", shirt, "--intent", L_ASK]) == 2
    assert "below the floor L" in capsys.readouterr().err


def test_tshirt_at_floor_is_not_refused_by_the_gate(capsys):
    _main(["--appetite", "large", "--tshirt", "L", "--intent", L_ASK])
    assert "below the floor" not in capsys.readouterr().err


def test_single_part_ask_floors_at_xs(capsys):
    rc, out = _floor(capsys, "P-A only")
    assert rc == 0
    assert json.loads(out)["tshirt_floor"] == "XS"
