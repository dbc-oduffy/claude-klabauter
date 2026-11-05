"""requirements_ledger.judge refuses met on a row citing an external original verify never read."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261105T000000-abc123"
REL = "state/sizings/2026-11-05-original.yaml"
URL = "https://claude.ai/artifact/abc123"


@pytest.fixture
def ledger_and_root(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": f"match the design at {URL} exactly"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    ledger = rl.read_ledger(tmp_path, tmp_path / RECORD_DIR_ROOT / RUN)
    cited = next(i for i in ledger.items if URL in i.text)
    return tmp_path, ledger, cited


def _judge(root, ledger, cited, **item):
    items = [{"id": i.id, "status": "met"} for i in ledger.items if i is not cited]
    items.append({"id": cited.id, "status": "met", **item})
    return rl.judge(ledger, {"items": items}, repo_root=root)


def _blocked(out):
    return [f for f in out["failures"] if f.get("status") == "BLOCKED"]


def test_met_without_originals_read_is_blocked(ledger_and_root):
    root, ledger, cited = ledger_and_root
    out = _judge(root, ledger, cited)
    assert out["pass"] is False
    (blocked,) = _blocked(out)
    assert blocked["id"] == cited.id
    assert blocked["reason"].startswith("BLOCKED: unreachable-original")
    assert URL in blocked["reason"]
    assert blocked["capture_age_s"] is None
    assert "capture age unknowable" in blocked["reason"]


def test_met_with_originals_read_passes(ledger_and_root):
    root, ledger, cited = ledger_and_root
    out = _judge(root, ledger, cited, originals_read=[URL])
    assert out["pass"] is True, out
    assert not _blocked(out)


def test_capture_age_stamped_when_both_stamps_present(ledger_and_root):
    root, ledger, cited = ledger_and_root
    out = _judge(
        root, ledger, cited,
        capture_harvested_at="2026-11-05T10:00:00Z", original_last_edited_at="2026-11-05T10:01:30Z",
    )
    (blocked,) = _blocked(out)
    assert blocked["capture_age_s"] == 90
    assert "capture age 90s" in blocked["reason"]


def test_not_met_row_is_not_double_blocked(ledger_and_root):
    root, ledger, cited = ledger_and_root
    items = [{"id": i.id, "status": "met"} for i in ledger.items if i is not cited]
    items.append({"id": cited.id, "status": "not_met", "remaining": ["x"]})
    out = rl.judge(ledger, {"items": items}, repo_root=root)
    assert not _blocked(out)
